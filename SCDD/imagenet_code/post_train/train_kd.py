import os
# os.environ["CUDA_VISIBLE_DEVICES"] = '0'
import sys
import math
import time
import shutil
import argparse
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import torchvision.datasets as datasets
import torchvision.transforms as transforms
from torchvision.transforms import InterpolationMode
from torch.optim.lr_scheduler import LambdaLR
import torchvision.models as models

from utils import AverageMeter, accuracy, get_parameters, mix_aug
from dataset_v2 import SubImageFolder
import timm 
from torch.utils.data._utils.fetch import _MapDatasetFetcher

def get_args():
    parser = argparse.ArgumentParser("FKD Training on ImageNet-1K")
    parser.add_argument('--batch-size', type=int,
                        default=1024, help='batch size')
    parser.add_argument('--gradient-accumulation-steps', type=int,
                        default=1, help='gradient accumulation steps for small gpu memory')
    parser.add_argument('--start-epoch', type=int,
                        default=0, help='start epoch')
    parser.add_argument('--epochs', type=int, default=300, help='total epoch')
    parser.add_argument('-j', '--workers', default=8, type=int,
                        help='number of data loading workers')

    parser.add_argument('--train-dir', type=str, default=None,
                        help='path to training dataset')
    parser.add_argument('--val-dir', type=str,
                        default='/your/path/imagenet/val', help='path to validation dataset')
    parser.add_argument('--output-dir', type=str,
                        default='./save', help='path to output dir')

    parser.add_argument('--cos', default=False,
                        action='store_true', help='cosine lr scheduler')
    parser.add_argument('--sgd', default=False,
                        action='store_true', help='sgd optimizer')
    parser.add_argument('-lr', '--learning-rate', type=float,
                        default=1.024, help='sgd init learning rate')  # checked
    parser.add_argument('--momentum', type=float,
                        default=0.875, help='sgd momentum')  # checked
    parser.add_argument('--weight-decay', type=float,
                        default=3e-5, help='sgd weight decay')  # checked
    parser.add_argument('--adamw-lr', type=float,
                        default=0.001, help='adamw learning rate')
    parser.add_argument('--adamw-weight-decay', type=float,
                        default=0.01, help='adamw weight decay')
    parser.add_argument('--model', type=str,
                        default='resnet18', help='student model name')
    parser.add_argument('-T', '--temperature', type=float,
                        default=20.0, help='temperature for distillation loss')
    parser.add_argument('--mix-type', default=None, type=str,
                        choices=['mixup', 'cutmix', None], help='mixup or cutmix or None')
    parser.add_argument('--mixup', type=float, default=0.8,
                    help='mixup alpha, mixup enabled if > 0. (default: 0.8)')
    parser.add_argument('--cutmix', type=float, default=1.0,
                    help='cutmix alpha, cutmix enabled if > 0. (default: 1.0)')

    ## add for selection policy
    parser.add_argument('--teacher-model', type=str, default=None,
                        help='teacher model name')
    parser.add_argument('--teacher-model-pretrained',type=str,default='',
                        help='dir of the pretrained model of teacher model, namely moco+cls')
    parser.add_argument('--base-model',type=str,default='resnet50',
                        help='basic model')
    parser.add_argument('--my-device',type=str,default='cuda:0')
    args = parser.parse_args()

    args.mode = 'fkd_save' 
    return args

def main():
    args = get_args()

    print('args.output_dir')
    print(args.output_dir)
    print(args.output_dir.split('/')[-1])


    if not torch.cuda.is_available():
        raise Exception("need gpu to train!")


    assert os.path.exists(args.train_dir)
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    # Data loading
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    train_transforms = transforms.Compose([
        transforms.RandomResizedCrop(224, scale=(0.08, 1.0)),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        normalize])

    train_dataset = SubImageFolder(
        args.train_dir,
        transform=train_transforms,
        image_index=None
    )

    sampler = None

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=(sampler is None), sampler=sampler,
        num_workers=args.workers, pin_memory=True)

    # load validation data
    val_loader = torch.utils.data.DataLoader(
        datasets.ImageFolder(args.val_dir, transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            normalize,
        ])),
        batch_size=int(args.batch_size/4), shuffle=False,
        num_workers=args.workers, pin_memory=True)
    print('load data successfully')


    # load student model
    print("=> loading student model '{}'".format(args.model))
    model = torchvision.models.__dict__[args.model](pretrained=False)
    model = nn.DataParallel(model).cuda()
    model.train()

    # load teacher model
    print("=> loading teacher model '{}'".format(args.teacher_model))
    teacher_model = torchvision.models.__dict__[args.teacher_model](pretrained=True)
    teacher_model = nn.DataParallel(teacher_model).cuda()
    teacher_model.eval()
    for param in teacher_model.parameters():
        param.requires_grad = False
    args.teacher_model = teacher_model

    if args.sgd:
        optimizer = torch.optim.SGD(get_parameters(model),
                                    lr=args.learning_rate,
                                    momentum=args.momentum,
                                    weight_decay=args.weight_decay)
    else:
        optimizer = torch.optim.AdamW(get_parameters(model),
                                      lr=args.adamw_lr,
                                      weight_decay=args.adamw_weight_decay)

    if args.cos == True:
        scheduler = LambdaLR(optimizer,
                             lambda step: 0.5 * (1. + math.cos(math.pi * step / args.epochs)) if step <= args.epochs else 0, last_epoch=-1)
    else:
        scheduler = LambdaLR(optimizer,
                             lambda step: (1.0-step/args.epochs) if step <= args.epochs else 0, last_epoch=-1)


    args.best_acc1=0
    args.optimizer = optimizer
    args.scheduler = scheduler
    args.train_loader = train_loader
    args.val_loader = val_loader

    max_prob_crop_list = []
    for epoch in range(args.start_epoch, args.epochs):
        print(f"\nEpoch: {epoch}")

        max_prob_crop = train(model, args, epoch)
        max_prob_crop_list.append(max_prob_crop)

        if epoch % 10 == 0 or epoch == args.epochs - 1:
            top1 = validate(model, args, epoch)
        else:
            top1 = 0

        scheduler.step()

        # remember best acc@1 and save checkpoint
        is_best = top1 > args.best_acc1
        args.best_acc1 = max(top1, args.best_acc1)
        save_checkpoint({
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'best_acc1': args.best_acc1,
            'optimizer' : optimizer.state_dict(),
            'scheduler' : scheduler.state_dict(),
        }, is_best, output_dir=args.output_dir)


def train(model, args, epoch=None):
    objs = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    optimizer = args.optimizer
    scheduler = args.scheduler
    loss_function_kl = nn.KLDivLoss(reduction='batchmean')

    model.train()
    args.teacher_model.eval()
    t1 = time.time()
    max_prob_list = []
    for batch_idx, (data, target) in enumerate(args.train_loader):
        target = target.type(torch.LongTensor)
        data, target = data.cuda(), target.cuda()

        images, _, _, _ = mix_aug(data, args)

        output = model(images)
        soft_label = args.teacher_model(images).detach()
        prec1, prec5 = accuracy(output, target, topk=(1, 5))
        output = F.log_softmax(output/args.temperature, dim=1)
        soft_label = F.softmax(soft_label/args.temperature, dim=1)

        max_prob, _ = torch.max(soft_label, dim=1)
        max_prob_list.append(max_prob.mean().item())

        loss = loss_function_kl(output, soft_label)

        n = images.size(0)
        objs.update(loss.item(), n)
        top1.update(prec1.item(), n)
        top5.update(prec5.item(), n)

        if batch_idx == 0:
            optimizer.zero_grad()

        if args.gradient_accumulation_steps > 1:
            loss = loss / args.gradient_accumulation_steps

        loss.backward()

        if (batch_idx + 1) % args.gradient_accumulation_steps == 0 or batch_idx == len(args.train_loader) - 1:
            optimizer.step()
            optimizer.zero_grad()

    printInfo = 'TRAIN Iter {}: lr = {:.6f},\tloss = {:.6f},\t'.format(epoch, scheduler.get_last_lr()[0], objs.avg) + \
                'Top-1 err = {:.6f},\t'.format(100 - top1.avg) + \
                'Top-5 err = {:.6f},\t'.format(100 - top5.avg) + \
                'train_time = {:.6f}'.format((time.time() - t1))
    print(printInfo)
    t1 = time.time()
    return np.mean(max_prob_list)


def validate(model, args, epoch=None):
    objs = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
    loss_function = nn.CrossEntropyLoss()

    model.eval()
    t1  = time.time()
    with torch.no_grad():
        for data, target in args.val_loader:
            target = target.type(torch.LongTensor)
            data, target = data.cuda(), target.cuda()

            output = model(data)
            loss = loss_function(output, target)

            prec1, prec5 = accuracy(output, target, topk=(1, 5))
            n = data.size(0)
            objs.update(loss.item(), n)
            top1.update(prec1.item(), n)
            top5.update(prec5.item(), n)

    logInfo = 'TEST Iter {}: loss = {:.6f},\t'.format(epoch, objs.avg) + \
              'Top-1 err = {:.6f},\t'.format(100 - top1.avg) + \
              'Top-5 err = {:.6f},\t'.format(100 - top5.avg) + \
              'val_time = {:.6f}'.format(time.time() - t1)
    print(logInfo)

    return top1.avg

def save_checkpoint(state, is_best, output_dir=None,epoch=None):
    if epoch is None:
        path = output_dir + '/' + 'checkpoint.pth.tar'
    else:
        path = output_dir + f'/checkpoint_{epoch}.pth.tar'
    torch.save(state, path)

    if is_best:
        path_best = output_dir + '/' + 'model_best.pth.tar'
        shutil.copyfile(path, path_best)


def load_image_index(args):
    if args.image_select_idx == -1:
        return None
    rng = np.random.RandomState(args.image_select_idx)
    index_list = [rng.choice(200, 4, replace=False) for _ in range(1000)]
    index_matrix = np.vstack(index_list)
    return index_matrix


if __name__ == "__main__":
    import multiprocessing as mp
    mp.set_start_method('spawn')
    main()