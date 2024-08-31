import os
import sys
import datetime
import logging
import argparse
from numbers import Number
from typing import List, Dict, Tuple, Optional, Literal, Union, Any, Callable

from tqdm import tqdm
from safetensors.torch import save_file, load_file
from scipy.stats import pearsonr, spearmanr

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, Sampler
from torch.utils.tensorboard.writer import SummaryWriter

from dataset import StockDataset, StockSequenceDataset, RandomSampleSampler
from nets import AttnRet
from loss import MSE_Loss
from utils import str2bool, str2dtype, str2device

class AttnRetTrainer:
    """FactorVAE Trainer，用于训练和评估一个基于因子变分自编码器（FactorVAE）的模型"""
    def __init__(self,
                 model:AttnRet,
                 loss_func:MSE_Loss = None,
                 optimizer:torch.optim.Optimizer = None,
                 lr_scheduler:Optional[torch.optim.lr_scheduler.LRScheduler] = None,
                 dtype:torch.dtype = torch.float32,
                 device:torch.device=torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")) -> None:
        
        self.model:AttnRet = model # FactorVAE 模型实例
        self.loss_func:nn.Module = loss_func # 损失函数实例
        self.optimizer:nn.Module = optimizer # 优化器实例
        self.lr_scheduler:Optional[torch.optim.lr_scheduler.LRScheduler] = lr_scheduler # 学习率调度器实例

        self.train_loader:DataLoader
        self.val_loader:DataLoader

        self.writer:SummaryWriter = None

        self.max_epoches:int
        self.hparams:Optional[dict]

        self.log_folder:str = "log"
        self.sample_per_batch:int = 0
        self.report_per_epoch:int = 1
        self.save_per_epoch:int = 1
        self.save_folder:str = "weights"
        self.save_name:str = "Model"
        self.save_format:Literal[".pt", ".safetensors"] = ".pt"
        
        self.dtype:torch.dtype = dtype
        self.device:torch.device = device
        
    def load_dataset(self, 
                     train_set:StockSequenceDataset, 
                     val_set:StockSequenceDataset,
                     batch_size:Optional[int] = None,
                     sampler:Optional[Sampler] = None,
                     shuffle:bool = True,
                     num_workers:int = 4):
        # 数据集加载
        if sampler is not None:
            self.train_loader = DataLoader(dataset=train_set,
                                        batch_size=batch_size, 
                                        sampler=sampler,
                                        num_workers=num_workers,)
        else:
            self.train_loader = DataLoader(dataset=train_set,
                                        batch_size=batch_size, 
                                        shuffle=shuffle,
                                        num_workers=num_workers)
        self.val_loader = DataLoader(dataset=val_set, 
                                    batch_size=batch_size,
                                    shuffle=shuffle,
                                    num_workers=num_workers)
        
    def save_checkpoint(self, 
                        save_folder:str, 
                        save_name:str, 
                        save_format:Literal[".pt",".safetensors"]=".pt"):
        # 模型保存
        save_path = os.path.join(save_folder, save_name+save_format)
        if save_format == ".pt":
            torch.save(self.model.state_dict(), save_path)
        elif save_format == ".safetensors":
            save_file(self.model.state_dict(), save_path)

    def load_checkpoint(self,
                        model_path:str):
        # 模型加载
        if model_path.endswith(".pt"):
            self.model.load_state_dict(torch.load(model_path))
        elif model_path.endswith(".safetensors"):
            self.model.load_state_dict(load_file(model_path))

    def set_configs(self,
                    max_epoches:int,
                    hparams:Optional[dict] = None,
                    log_folder:str = "log",
                    sample_per_batch:int = 0,
                    report_per_epoch:int=1,
                    save_per_epoch:int=1,
                    save_folder:str=os.curdir,
                    save_name:str="Model",
                    save_format:str=".pt"):
        # 配置设置
        self.max_epoches = max_epoches
        self.hparams = hparams
        
        self.log_folder = log_folder
        self.sample_per_batch = sample_per_batch
        self.report_per_epoch = report_per_epoch
        self.save_per_epoch = save_per_epoch
        self.save_folder = save_folder
        self.save_name = save_name
        self.save_format = save_format

        if not os.path.exists(self.log_folder):
            os.makedirs(self.log_folder)
        if not os.path.exists(self.save_folder):
            os.makedirs(self.save_folder)

        self.writer = SummaryWriter(
            os.path.join(
                self.log_folder, f"TRAIN_{self.save_name}_{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
            ))
        
    def save_configs(self, save_path:Optional[str]=None): #TODO
        if save_path is None:
            save_path = os.path.join(self.save_folder, "config.json")

        config = {
            "model": {
                "type": "AttnRet",
                "fundamental_feature_size": self.hparams["fundamental_feature_size"], 
                "quantity_price_feature_size": self.hparams["quantity_price_feature_size"],
                "num_gru_layers": self.hparams["num_gru_layers"], 
                "gru_hidden_size": self.hparams["gru_hidden_size"], 
                "gru_drop_out": self.hparams["gru_dropout"],
                "num_fc_layers": self.hparams["num_fc_layers"], 
                "pretrained": self.hparams["checkpoint"]},
            "optimizer": {
                "type": "Adam",
                "learning_rate": self.hparams["lr"]},
            "loss": {
                "type": "MSE", 
                "scale": self.hparams["sacle"]},
            "max_epoches": self.max_epoches,
            "num_batches_per_epoch": self.hparams["num_batches_per_epoch"],
            "device": self.device,
            "dtype": self.dtype,
            }
        
    def train(self):
        writer = self.writer
        model = self.model.to(device=self.device, dtype=self.dtype)
        loss_func = self.loss_func
        optimizer = self.optimizer

        # 主训练循环
        for epoch in range(self.max_epoches):
            train_loss_list = []
            val_loss_list = []
            
            # 每个epoch上的训练
            model.train()
            for batch, (quantity_price_feature, fundamental_feature, label) in enumerate(tqdm(self.train_loader)):
                optimizer.zero_grad() # 梯度归零
                if fundamental_feature.shape[0] == 0:
                    continue

                quantity_price_feature = quantity_price_feature.to(device=self.device, dtype=self.dtype)
                fundamental_feature = fundamental_feature.to(device=self.device, dtype=self.dtype)
                label = label.to(device=self.device, dtype=self.dtype)

                y_pred = model(fundamental_feature, quantity_price_feature) # 模型运算
                train_loss = loss_func(label, y_pred) # 损失函数计算
                
                train_loss.backward() # 梯度反向传播
                optimizer.step() # 优化器更新模型权重
                train_loss_list.append(train_loss.item()) # 记录训练损失
                
                # 训练时抽样检查
                if self.sample_per_batch:
                    if (batch+1) % self.sample_per_batch == 0:
                        logging.debug(f"<Batch {batch+1}>  loss:{train_loss.item()} y_hat:{y_pred} y_true:{label}")
              
            # Tensorboard写入当前完成epoch的训练损失
            train_loss_epoch = sum(train_loss_list)/len(train_loss_list)
            writer.add_scalar("Train Loss", train_loss_epoch, epoch+1)
            
            # 交叉验证集上验证（无梯度）
            model.eval() # 设置为eval模式以冻结dropout
            with torch.no_grad(): 
                for batch, (quantity_price_feature, fundamental_feature, label) in enumerate(self.val_loader):
                    quantity_price_feature = quantity_price_feature.to(device=self.device, dtype=self.dtype)
                    fundamental_feature = fundamental_feature.to(device=self.device, dtype=self.dtype)
                    label = label.to(device=self.device, dtype=self.dtype)
                    y_pred = model(fundamental_feature, quantity_price_feature, label)
                    val_loss = loss_func(label, y_pred)
                    val_loss_list.append(val_loss.item())

                val_loss_epoch = sum(val_loss_list) / len(val_loss_list)  
                writer.add_scalar("Validation Loss", val_loss_epoch, epoch+1)
                writer.add_scalars("Train-Val Loss", {"Train Loss": train_loss_epoch, "Validation Loss": val_loss_epoch}, epoch+1)

            # 如果有超参数字典传入，Tensorboard记录超参数
            if self.hparams:
                writer.add_hparams(hparam_dict=self.hparams, metric_dict={"hparam/TrainLoss":train_loss_epoch, "hparam/ValLoss":val_loss_epoch})

            # 如果有学习率调度器传入，则更新之。
            writer.add_scalar("Learning Rate", optimizer.param_groups[0]["lr"], epoch+1)
            if self.lr_scheduler:
                self.lr_scheduler.step()
            
            # Tensorboard写入磁盘
            writer.flush()

            # 打印每个epoch训练结果
            if self.report_per_epoch:
                if (epoch+1) % self.report_per_epoch == 0:
                    logging.info('Epoch [{}/{}], Train Loss: {:.6f}, Validation Loss: {:.6f}'.format(epoch+1, self.max_epoches, train_loss_epoch, val_loss_epoch))
            
            # 保存模型
            if self.save_per_epoch:
                if (epoch+1) % self.save_per_epoch == 0:
                    model_name = f"{self.save_name}_epoch{epoch+1}"
                    self.save_checkpoint(save_folder=self.save_folder,
                                         save_name=model_name,
                                         save_format=self.save_format)
                    logging.info(f"Epoch {epoch+1} Model weights saved to {os.path.join(self.save_folder)}")

        writer.close()


def parse_args():
    parser = argparse.ArgumentParser(description="FactorVAE Training.")

    parser.add_argument("--log_folder", type=str, default=os.curdir, help="Path of folder for log file. Default `.`")
    parser.add_argument("--log_name", type=str, default="log.txt", help="Name of log file. Default `log.txt`")

    parser.add_argument("--dataset_path", type=str, required=True, help="Path of dataset .pt file")
    parser.add_argument("--checkpoint_path", type=str, default=None, help="Path of checkpoint. Optional")

    parser.add_argument("--quantity_price_feature_size", type=int, required=True, help="Input size of quantity-price feature")
    parser.add_argument("--fundamental_feature_size", type=int, required=True, help="Input size of fundamental feature")
    parser.add_argument("--num_gru_layers", type=int, required=True, help="Num of GRU layers in feature extractor.")
    parser.add_argument("--gru_hidden_size", type=int, required=True, help="Hidden size of each GRU layer. num_gru_layers * gru_hidden_size i.e. the input size of FactorEncoder and Factor Predictor.")
    parser.add_argument("--gru_dropout", type=float, default=0.1, help="Dropout probs in gru layers. Default 0.1")
    parser.add_argument("--num_fc_layers", type=int, required=True, help="Num of full connected layers in MLP")

    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate for optimizer. Default 0.001")
    parser.add_argument("--scale", type=float, default=100, help="Scale for MSE Loss. Default 100")
    parser.add_argument("--num_workers", type=int, default=4, help="Num of subprocesses to use for data loading. 0 means that the data will be loaded in the main process. Default 4")
    parser.add_argument("--shuffle", type=str2bool, default=True, help="Whether to shuffle dataloader. Default True")
    parser.add_argument("--num_batches_per_epoch", type=int, default=-1, help="Num of batches sampled from all batches to be trained per epoch. Note that sampler option is mutually exclusive with shuffle. Specify -1 to disable (use all batches). Default -1")
    
    parser.add_argument("--max_epoches", type=int, default=20, help="Max Epoches for train loop")
    parser.add_argument("--sample_per_batch", type=int, default=0, help="Check X, y and all kinds of outputs per n batches in one epoch. Specify 0 to unable. Default 0")
    parser.add_argument("--report_per_epoch", type=int, default=1, help="Report train loss and validation loss per n epoches. Specify 0 to unable. Default 1")
    
    parser.add_argument("--dtype", type=str2dtype, default="FP32", help="Dtype of data and weight tensor. Literally `FP32`, `FP64`, `FP16` or `BF16`. Default `FP32`")
    parser.add_argument("--device", type=str2device, default="cuda", help="device to take calculation. Literally `cpu` or `cuda`. Default `cuda`")

    parser.add_argument("--save_per_epoch", type=int, default=1, help="Save model weights per n epoches. Specify 0 to unable. Default 1")
    parser.add_argument("--save_folder", type=str, required=True, help="Folder to save model")
    parser.add_argument("--save_name", type=str, default="Model", help="Model name. Default `Model`")
    parser.add_argument("--save_format", type=str, default=".pt", help="File format of model to save, literally `.pt` or `.safetensors`. Default `.pt`")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    os.makedirs(args.log_folder, exist_ok=True)
    os.makedirs(args.save_folder, exist_ok=True)
    os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"

    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - [%(levelname)s] : %(message)s',
        handlers=[logging.FileHandler(os.path.join(args.log_folder, args.log_name)), logging.StreamHandler()])
    
    logging.debug(f"Command: {' '.join(sys.argv)}")
    logging.debug(f"Params: {vars(args)}")

    datasets:Dict[str, StockSequenceDataset] = torch.load(args.dataset_path)
    train_set = datasets["train"]
    val_set = datasets["val"]
    test_set = datasets["test"]
    if args.num_batches_per_epoch != -1:
        train_sampler = RandomSampleSampler(train_set, args.num_batches_per_epoch)
    else:
        train_sampler = None

    model = AttnRet(fundamental_feature_size=args.fundamental_feature_size, 
                    quantity_price_feature_size=args.quantity_price_feature_size,
                    num_gru_layers=args.num_gru_layers, 
                    gru_hidden_size=args.gru_hidden_size, 
                    gru_drop_out=args.gru_dropout,
                    num_fc_layers=args.num_fc_layers)

    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    loss_func = MSE_Loss(scale=args.scale)

    hparams = {"fundamental_feature_size":args.fundamental_feature_size, 
               "quantity_price_feature_size":args.quantity_price_feature_size,
               "num_gru_layers": args.num_gru_layers, 
               "gru_hidden_size": args.gru_hidden_size, 
               "gru_drop_out": args.gru_dropout,
               "num_fc_layers": args.num_fc_layer,
               "checkpoint": args.checkpoint_path,
               "lr": args.lr,
               "scale": args.scale, 
               "num_batches_per_epoch":args.num_batches_per_epoch}

    trainer = AttnRetTrainer(model=model,
                             loss_func=loss_func,
                             optimizer=optimizer,
                             dtype=args.dtype,
                             device=args.device)
    trainer.load_dataset(train_set=train_set, 
                         val_set=val_set, 
                         shuffle=args.shuffle, 
                         sampler=train_sampler,
                         num_workers=args.num_workers)
    if args.checkpoint_path is not None:
        trainer.load_checkpoint(args.checkpoint_path)
    trainer.set_configs(max_epoches=args.max_epoches,
                        log_folder=args.log_folder,
                        sample_per_batch=args.sample_per_batch,
                        report_per_epoch=args.report_per_epoch,
                        save_per_epoch=args.save_per_epoch,
                        save_folder=args.save_folder,
                        save_name=args.save_name,
                        save_format=args.save_format,
                        hparams=hparams)
    logging.info("Training start...")
    trainer.train()
    logging.info("Training complete.")

# python train.py --log_folder "D:\PycharmProjects\SWHY\log\FactorVAE" --log_name "Model1" --dataset_path "D:\PycharmProjects\SWHY\data\preprocess\dataset.pt" --input_size 101 --num_gru_layers 4 --gru_hidden_size 32 --hidden_size 8 --latent_size 2 --save_folder "D:\PycharmProjects\SWHY\model\factor-vae\model1" --save_name "model1" --save_format ".pt" --sample_per_batch 100

# python train.py --log_folder "D:\PycharmProjects\SWHY\log\FactorVAE" --log_name "Model4" --dataset_path "D:\PycharmProjects\SWHY\data\preprocess\dataset_cs_zscore.pt" --input_size 101 --num_gru_layers 2 --gru_hidden_size 32 --hidden_size 16 --latent_size 4 --save_folder "D:\PycharmProjects\SWHY\model\factor-vae\model1" --save_name "model4" --save_format ".pt" --sample_per_batch 200

# python train.py --log_folder "D:\PycharmProjects\SWHY\log\FactorVAE" --log_name "Model5" --dataset_path "D:\PycharmProjects\SWHY\data\preprocess\dataset.pt" --input_size 101 --num_gru_layers 2 --gru_hidden_size 32 --hidden_size 16 --latent_size 4 --save_folder "D:\PycharmProjects\SWHY\model\factor-vae\model5" --save_name "model5" --save_format ".pt" --sample_per_batch 50 --num_batches_per_epoch 200

# python train.py --log_folder "D:\PycharmProjects\SWHY\log\FactorVAE" --log_name "Model8.txt" --dataset_path "D:\PycharmProjects\SWHY\data\preprocess\dataset.pt" --input_size 101 --num_gru_layers 4 --gru_hidden_size 32 --hidden_size 100 --latent_size 48 --gru_dropout 0.1 --std_activation "exp" --save_folder "D:\PycharmProjects\SWHY\model\factor-vae\model8" --save_name "model8" --save_format ".pt" --sample_per_batch 50 --num_batches_per_epoch 200

# python train.py --log_folder "log" --log_name "Model8.txt" --dataset_path "dataset.pt" --quantity_price_feature_size 101 --fundamental_feature_size 31 --num_gru_layers 4 --gru_hidden_size 32 --hidden_size 100 --latent_size 48 --gru_dropout 0.1 --std_activation "exp" --save_folder "D:\PycharmProjects\SWHY\model\factor-vae\model8" --save_name "model_attn_1" --save_format ".pt" --sample_per_batch 50 --num_batches_per_epoch 200