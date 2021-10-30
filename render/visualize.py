import argparse
import numpy as np
import os
import random
import tensorboardX
import time
import torch
import torch.nn as nn
import torchvision
import config
import model
import pickle as pkl
import sys
import cv2
import lmdb
sys.path.append("..")
import deep_3drecon
from trainer import TuneTrainer
from io import BytesIO
from utils import gen_randomwalk_list
from loader import TuneTestset
from utils import worker_init_fn
from torch.utils.data import DataLoader
from tqdm import tqdm

parser = argparse.ArgumentParser()
parser.add_argument('--texture_dim', type=int, default=config.TEXTURE_DIM)
parser.add_argument('--it', type=int, default=config.ITERATIONS)
parser.add_argument('--batch', type=int, default=config.BATCH_SIZE)
parser.add_argument('--lr', type=float, default=config.LEARNING_RATE)
parser.add_argument('-tl', '--train_len', type=int, default=config.TRAIN_LEN)
args = parser.parse_args()

# 全局init model
tex_sampler = nn.DataParallel(model.TexSampler().cuda())
face_unet = nn.DataParallel(model.define_G(args.texture_dim, 3, 64, 'local').cuda())
blender = nn.DataParallel(model.define_G(3, 3, 64, 'local').cuda())
bgerode = model.BgErode().cuda()
tex_sampler.eval()
face_unet.eval()
blender.eval()
texture = None

def visualize(lmdb_path, name, model_path):
    def getvideo(file_path):
        cap = cv2.VideoCapture(file_path)
        frame_list = []
        while cap.isOpened():
            ret, frame = cap.read()
            if ret == False:
                break
            frame_list.append(frame)
        return frame_list

    global args, tex_sampler, face_unet, blender, texture, bgerode
    args.name = name
    args.model_path = model_path
    face_unet.module.load_state_dict(torch.load(os.path.join(model_path, 'face_unet.pkl')))
    blender.module.load_state_dict(torch.load(os.path.join(model_path, 'blender.pkl')))
    # face_unet.module.load_state_dict(torch.load("/home/wuhz/mnt/avatar/style_avatar/render/model/render_lrw_lr_2e-4_tl_1/face_unet.pkl"))
    # blender.module.load_state_dict(torch.load("/home/wuhz/mnt/avatar/style_avatar/render/model/render_lrw_lr_2e-4_blend_lap/generator.pkl"))
    texture = torch.load(os.path.join(model_path, 'neural_texture.pkl'), map_location=torch.device('cuda:0'))
    # for i in range(len(texture[0])):
    #     torchvision.utils.save_image(texture[0, i], "../visualize/tex_{}.png".format(i), normalize = True, range = (-1, 1))

    # # 给coeff, audio合成视频
    # # 要重建一下ted hd中测试得到的参数看看音频驱动的效果，只选取一个唇形比较准的风格即可(用4号coeff)
    face_reconstructor = deep_3drecon.Reconstructor()
    torch.cuda.set_device(0)
    env = lmdb.open(lmdb_path, map_size=1099511627776, max_dbs = 64)
    coeff_db = env.open_db("coeff".encode())
    align_db = env.open_db("align".encode())
    uv_db = env.open_db("uv".encode())
    bg_db = env.open_db("bg".encode())
    txn = env.begin(write = False)
    lmdb_length = txn.stat(db=align_db)['entries']

    coeff_bin = txn.get(str(0).encode(), db = coeff_db)
    coeff = pkl.load(BytesIO(coeff_bin))

    align_bin = txn.get(str(0).encode(), db = align_db)
    with open('test_align.mp4', 'wb') as f:
        f.write(align_bin)
    align_frames = np.array(getvideo('test_align.mp4'))

    # 从另一个lmdb path得到audio
    ted_lmdb_path = "../data/ted_hd/lmdb"
    ted_env = lmdb.open(ted_lmdb_path, map_size=1099511627776, max_dbs = 64)
    audio_db = ted_env.open_db("test_audio".encode())
    ted_txn = ted_env.begin(write = False)

    # 先生成数据
    for i in tqdm(range(1)):
        dst_coeff = np.load("../data/ted_hd/test/{}_0.npy".format(i))
        test_index = gen_randomwalk_list(list(range(len(coeff))), len(dst_coeff))
        test_coeff = coeff[test_index]
        test_coeff[:, 80: 144] = dst_coeff[:, 80: 144]
        test_align = align_frames[test_index]
        face_reconstructor.recon_uv_from_coeff(test_coeff, out_path = "test_uv.mp4", bg_path = "test_bg.mp4")
        test_uv = np.array(getvideo('test_uv.mp4'))
        test_bg = np.array(getvideo('test_bg.mp4'))
        
        test_audio_bin = ted_txn.get(str(i).encode(), db = audio_db)
        test_audio_path = "./test.wav"
        with open(test_audio_path, 'wb') as f:
            f.write(test_audio_bin)
        
        # 将数据convert成torch tensor，然后跑render
        testset = TuneTestset(test_uv, test_bg, test_align)
        testloader = DataLoader(testset, batch_size = 16, shuffle = False,
                                pin_memory = True, drop_last = False, num_workers = 16, worker_init_fn=worker_init_fn)

        with torch.no_grad():
            testloader_iter = iter(testloader)
            pred_img_batch = torch.zeros((len(testset), 3, 224, 224)).float()
            idx = 0
            while True:
                try:
                    uv_batch, bg_batch, align_batch = next(testloader_iter)
                except Exception:
                    break

                uv_batch = uv_batch.cuda()
                bg_batch = bg_batch.cuda()
                align_batch = align_batch.cuda()
                tex = texture.repeat(len(align_batch), 1, 1, 1)
                bg_erode = bgerode(bg_batch).detach()

                sample_image = tex_sampler(uv_batch, tex)
                pred_image_mask = face_unet(sample_image) * (1 - bg_batch)
                in_im_pred = pred_image_mask + bg_erode * align_batch
                if idx == 0:
                    bg_vis = bg_erode * align_batch
                    for i in range(len(sample_image[0])):
                        torchvision.utils.save_image(sample_image[0, i], "../visualize/latent_{}.png".format(i), normalize = True, range = (-1, 1))
                    torchvision.utils.save_image(torch.flip(pred_image_mask[0], dims = [0]), "../visualize/pred.png", normalize = True, range = (-1, 1))
                    torchvision.utils.save_image(torch.flip(bg_vis[0], dims = [0]), "../visualize/bg0.png", normalize = True, range = (-1, 1))
                    torchvision.utils.save_image(torch.flip(bg_vis[1], dims = [0]), "../visualize/bg1.png", normalize = True, range = (-1, 1))
                    torchvision.utils.save_image(torch.flip(bg_vis[2], dims = [0]), "../visualize/bg2.png", normalize = True, range = (-1, 1))
                    torchvision.utils.save_image(torch.flip(bg_batch[0], dims = [0]), "../visualize/bg_origin.png", normalize = True, range = (-1, 1))
                    torchvision.utils.save_image(torch.flip(bg_erode[0], dims = [0]), "../visualize/bg_erode.png", normalize = True, range = (-1, 1))
                    torchvision.utils.save_image(torch.flip(torch.logical_xor(bg_erode[0], bg_batch[0]).float(), dims = [0]), "../visualize/bg_xor.png", normalize = True, range = (-1, 1))
                    torchvision.utils.save_image(torch.flip(in_im_pred[0], dims = [0]), "../visualize/in.png", normalize = True, range = (-1, 1))
                    break
                pred_image = blender(in_im_pred) * (1 - bg_erode) + bg_erode * align_batch

                pred_img_batch[idx:idx + len(pred_image)] = pred_image.cpu()
                idx += len(pred_image)

            pred_img_batch = pred_img_batch.detach()
            pred_img_batch = torch.flip(pred_img_batch, dims = [1])
            # os.system("rm ../data/tmp/test/*")
            # for j in range(len(pred_img_batch)):
            #     torchvision.utils.save_image(pred_img_batch[j], "../data/tmp/test/{}.png".format(j), normalize = True, range = (-1, 1))
            # os.system("ffmpeg -y -loglevel warning -framerate 25 -start_number 0 -i ../data/tmp/test/%d.png -c:v libx264 -pix_fmt yuv420p -b:v 2000k ../data/demo/{}_{}.mp4".format(name, i))
            # os.system("ffmpeg -y -loglevel warning -i ../data/demo/{}_{}.mp4 -i {} -map 0:v -map 1:a -c:v copy -shortest ../data/demo/{}_{}_a.mp4".format(name, i, test_audio_path, name, i))

if __name__ == "__main__":


    # lmdb_path = "../data/demo_0"
    # model_path = "./ted_model/demo_0"
    # name = "demo_0"

    # visualize(lmdb_path, name, model_path)

    lmdb_path = "../data/demo_1"
    model_path = "./ted_model/demo_1"
    name = "demo_1"

    visualize(lmdb_path, name, model_path)

    # lmdb_path = "../data/demo_2"
    # model_path = "./ted_model/demo_2"
    # name = "demo_2"

    # visualize(lmdb_path, name, model_path)