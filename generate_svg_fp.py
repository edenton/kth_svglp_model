import torch
import torch.optim as optim
import torch.nn as nn
import argparse
import os
import random
from torch.autograd import Variable
from torch.utils.data import DataLoader
import utils
import itertools
import progressbar
import numpy as np
from scipy.ndimage.filters import gaussian_filter

parser = argparse.ArgumentParser()
parser.add_argument('--batch_size', default=100, type=int, help='batch size')
parser.add_argument('--data_root', default='/misc/vlgscratch3/FergusGroup/denton/data/KTH/processed/', help='root directory for data')
parser.add_argument('--model_path', default='pretrained_models/kth_svg_fp/model.pth', help='model directory')
parser.add_argument('--log_dir', default='logs/kth_svg_fp/', help='model directory')
parser.add_argument('--name', default='', help='additional string for filename')
parser.add_argument('--seed', default=1, type=int, help='manual seed')
parser.add_argument('--n_past', type=int, default=10, help='number of frames to condition on')
parser.add_argument('--n_future', type=int, default=20, help='number of frames to predict')
parser.add_argument('--num_threads', type=int, default=0, help='number of data loading threads')
parser.add_argument('--nsample', type=int, default=100, help='number of samples')
parser.add_argument('--N', type=int, default=256, help='number of samples')


opt = parser.parse_args()
os.makedirs('%s/gifs' % opt.log_dir, exist_ok=True)

opt.n_eval = opt.n_past+opt.n_future
opt.max_step = opt.n_eval

print("Random Seed: ", opt.seed)
random.seed(opt.seed)
torch.manual_seed(opt.seed)
torch.cuda.manual_seed_all(opt.seed)
dtype = torch.cuda.FloatTensor



# ---------------- load the models  ----------------
tmp = torch.load(opt.model_path)
lstm = tmp['lstm']
posterior = tmp['oracle']
lstm.eval()
encoder = tmp['encoder']
decoder = tmp['decoder']
#encoder.eval()
#decoder.eval()
lstm.batch_size = opt.batch_size
posterior.batch_size = opt.batch_size
opt.latent_dim = tmp['opt'].latent_dim
opt.posterior_dim = tmp['opt'].oracle_dim

# --------- transfer to gpu ------------------------------------
lstm.cuda()
posterior.cuda()
encoder.cuda()
decoder.cuda()

# ---------------- set the options ----------------
opt.dataset = tmp['opt'].dataset
opt.channels = tmp['opt'].channels
opt.image_width = tmp['opt'].image_width

print(opt)


# --------- load a dataset ------------------------------------
train_data, test_data = utils.load_dataset(opt)

train_loader = DataLoader(train_data,
                          num_workers=opt.num_threads,
                          batch_size=opt.batch_size,
                          shuffle=True,
                          drop_last=True,
                          pin_memory=True)
test_loader = DataLoader(test_data,
                         num_workers=opt.num_threads,
                         batch_size=opt.batch_size,
                         shuffle=True,
                         drop_last=True,
                         pin_memory=True)

def get_training_batch():
    while True:
        for sequence in train_loader:
            batch = utils.normalize_data(opt, dtype, sequence)
            yield batch
training_batch_generator = get_training_batch()

def get_testing_batch():
    while True:
        for sequence in test_loader:
            batch = utils.normalize_data(opt, dtype, sequence)
            yield batch 
testing_batch_generator = get_testing_batch()

# --------- eval funtions ------------------------------------

def make_gifs(x, idx):
    # get posterior
    lstm.hidden = lstm.init_hidden()
    posterior.hidden = posterior.init_hidden()
    posterior_gen = []
    posterior_gen.append(x[0])
    x_in = x[0]
    for i in range(1, opt.n_eval):
        h = encoder(x_in)
        h_target = encoder(x[i])
        if type(h) is tuple:
            if i < opt.n_past:
                h, skip = h
            else:
                h, _ = h
            h_target, _ = h_target
        h = h.view(-1, opt.latent_dim).detach()
        h_target = h_target.view(-1, opt.latent_dim).detach()
        z_t, _, _= posterior(h_target)
        if i < opt.n_past:
            lstm(torch.cat([h, z_t], 1)) 
            posterior_gen.append(x[i])
            x_in = x[i]
        else:
            h_pred = lstm(torch.cat([h, z_t], 1)).view(opt.batch_size, opt.latent_dim, 1, 1) 
            x_in = decoder([[h_pred, skip], []]).detach()
            posterior_gen.append(x_in)
  

    nsample = opt.nsample
    ssim = np.zeros((opt.batch_size, nsample, opt.n_future))
    psnr = np.zeros((opt.batch_size, nsample, opt.n_future))
    progress = progressbar.ProgressBar(max_value=nsample).start()
    all_gen = []
    for s in range(nsample):
        progress.update(s+1)
        gen_seq = []
        gt_seq = []
        lstm.hidden = lstm.init_hidden()
        posterior.hidden = posterior.init_hidden()
        x_in = x[0]
        all_gen.append([])
        all_gen[s].append(x_in)
        for i in range(1, opt.n_eval):
            h = encoder(x_in)
            if type(h) is tuple:
                if i < opt.n_past:	
                    h, skip = h
                else:
                    h, _ = h
            else:
                skip = []
            h = h.view(-1, opt.latent_dim).detach()
            if i + 1 < opt.n_past:
                h_target = encoder(x[i])
                if type(h_target) is tuple:
                    h_target = h_target[0]
                h_target = h_target.view(-1, opt.latent_dim).detach()
                z_t, _, _ = posterior(h_target)
            else:
                z_t = torch.cuda.FloatTensor(opt.batch_size, opt.posterior_dim).normal_()
            if i < opt.n_past:
                lstm(torch.cat([h, z_t], 1))
                x_in = x[i]
                all_gen[s].append(x_in)
            else:
                h = lstm(torch.cat([h, z_t], 1)).view(opt.batch_size, opt.latent_dim, 1, 1).detach()
                x_in = decoder([[h, skip], []]).detach()
                gen_seq.append(x_in.data.cpu().numpy())
                all_gen[s].append(x_in)
                gt_seq.append(x[i].data.cpu().numpy())
        _, ssim[:, s, :], psnr[:, s, :] = utils.eval_seq(gt_seq, gen_seq)

    progress.finish()
    utils.clear_progressbar()

    ###### ssim ######
    for i in range(opt.batch_size):
        gifs = [ [] for t in range(opt.n_eval) ]
        text = [ [] for t in range(opt.n_eval) ]
        mean_ssim = np.mean(psnr[i, :, :], 1)
        ordered = np.argsort(mean_ssim)
        for t in range(opt.n_eval):
            # gt 
            gifs[t].append(add_border(x[t][i], 'green'))
            text[t].append('Ground\ntruth')
            #posterior 
            if t < opt.n_past:
                color = 'green'
            else:
                color = 'red'
            gifs[t].append(add_border(posterior_gen[t][i], color))
            text[t].append('Approx.\nposterior')
            # best 
            if t < opt.n_past:
                color = 'green'
            else:
                color = 'red'
            sidx = ordered[-1]
            gifs[t].append(add_border(all_gen[sidx][t][i], color))
            text[t].append('Best PSNR')
            # random 3
            for s in range(3):
                sidx = s*20
                gifs[t].append(add_border(all_gen[sidx][t][i], color))
                text[t].append('Random\nsample %d' % (s+1))

        fname = '%s/gifs/best_psnr_%d.gif' % (opt.log_dir, idx+i) 
        utils.save_gif_with_text(fname, gifs, text)

def add_border(x, color, pad=1):
    w = x.size()[1]
    nc = x.size()[0]
    px = Variable(torch.zeros(3, w+2*pad+30, w+2*pad))
    if color == 'red':
        px[0] =0.7 
    elif color == 'green':
        px[1] = 0.7
    if nc == 1:
        for c in range(3):
            px[c, pad:w+pad, pad:w+pad] = x
    else:
        px[:, pad:w+pad, pad:w+pad] = x
    return px

for i in range(0, opt.N, opt.batch_size):
    x = next(testing_batch_generator)
    make_gifs(x, i)
    print(i)

