import os
import sys
import yaml
import logging
import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F

from pathlib import Path
from abc import ABC, abstractmethod
from tqdm import tqdm
from typing import Callable, Dict, List, Optional, Tuple, Union
from torch.utils.data import DataLoader

# TODO Ma che cazzo asterisco sta facendo qui?!!
from . import *

from .metrics import MetricTracker
from .factory import (
    _get_params_from_config,
    create_model,
    create_dataset,
    create_loss_function,
)

# TODO do something with imports, it's a mess. either all from n2v init, or all separately


class Engine(ABC):
    def __init__(self, cfg):
        self.cfg = cfg

    @abstractmethod
    def get_model(self):
        pass

    @abstractmethod
    def get_train_dataloader(self):
        pass

    @abstractmethod
    def get_predict_dataloader(self):
        pass

    @abstractmethod
    def train(self, args):
        pass

    @abstractmethod
    def train_single_epoch(self, args):
        pass

    @abstractmethod
    def predict(self, args):
        pass


class UnsupervisedEngine(Engine):
    def __init__(self, cfg_path: str) -> None:
        self.logger = logging.getLogger()
        set_logging(self.logger)
        self.cfg = self.parse_config(cfg_path)
        self.model = self.get_model()
        self.loss_func = self.get_loss_function()
        self.mean = None
        self.std = None
        self.device = get_device()
        # TODO all initializations of custom classes should be done here

    def parse_config(self, cfg_path: str) -> Dict:
        try:
            cfg = config_loader(cfg_path)
        except (FileNotFoundError, yaml.YAMLError):
            # TODO add custom exception for different cases
            raise yaml.YAMLError("Config file not found")
        cfg = ConfigValidator(**cfg)
        self.logger.info(f"Config parsing done. Using file: {cfg_path}")
        return cfg

    def log_metrics(self):
        if self.cfg.misc.use_wandb:
            try:  # TODO test wandb. add functionality
                import wandb

                wandb.init(project=self.cfg.experiment_name, config=self.cfg)
                self.logger.info("using wandb logger")
            except ImportError:
                self.cfg.misc.use_wandb = False
                self.logger.warning(
                    "wandb not installed, using default logger. try pip install wandb"
                )
                return self.log_metrics()
        else:
            self.logger.info("Using default logger")

    def get_model(self):
        return create_model(self.cfg)

    def train(self):
        # General func
        train_loader, self.mean, self.std = self.get_train_dataloader()
        eval_loader = self.get_val_dataloader()
        eval_loader.dataset.set_normalization(self.mean, self.std)
        optimizer, lr_scheduler = self.get_optimizer_and_scheduler()
        scaler = self.get_grad_scaler()
        self.logger.info(f"Starting training for {self.cfg.training.num_epochs} epochs")
        try:
            for epoch in range(
                self.cfg.training.num_epochs
            ):  # loop over the dataset multiple times
                self.logger.info(f"Starting epoch {epoch}")

                train_outputs = self.train_single_epoch(
                    train_loader,
                    optimizer,
                    scaler,
                    self.cfg.training.amp.toggle,
                    self.cfg.training.max_grad_norm,
                )

                # Perform validation step
                eval_outputs = self.evaluate(eval_loader, self.cfg.evaluation.metric)
                self.logger.info(
                    f'Validation loss for epoch {epoch}: {eval_outputs["loss"]}'
                )
                # Add update scheduler rule based on type
                lr_scheduler.step(eval_outputs["loss"])
                # TODO implement checkpoint naming
                self.save_checkpoint("checkpoint.pth", False)
                self.logger.info(f"Save checkpoint to ")  # TODO correct path

        except KeyboardInterrupt:
            self.logger.info("Training interrupted")

    def evaluate(self, eval_loader: torch.utils.data.DataLoader, eval_metric: str):
        self.model.eval()
        # TODO Isnt supposed to be called without train ?
        avg_loss = MetricTracker()
        avg_loss.reset()

        with torch.no_grad():
            for image, *auxillary in tqdm(eval_loader):
                outputs = self.model(image.to(self.device))
                loss = self.loss_func(outputs, *auxillary, self.device)
                avg_loss.update(loss.item(), image.shape[0])

        return {"loss": avg_loss.avg}

    def predict(self):
        self.model.to(self.device)
        self.model.eval()
        pred_loader = self.get_predict_dataloader()
        if not (self.mean and self.std):
            _, self.mean, self.std = self.get_train_dataloader()
        pred_loader.dataset.set_normalization(self.mean, self.std)
        self.stitch = pred_loader.dataset.patch_generator is not None
        avg_metric = MetricTracker()
        # TODO get whole image size or append to variable sized array, rename
        pred = np.zeros((1, 321, 481))
        tiles = []
        if self.stitch:
            self.logger.info("Starting tiled prediction")
        else:
            self.logger.info("Starting prediction on whole sample")
        with torch.no_grad():
            for image, *auxillary in tqdm(pred_loader):
                # TODO define all predict/train funcs in separate modules
                if auxillary:
                    (
                        sample,
                        tile_level_coords,
                        all_tiles_shape,
                        image_shape,
                    ) = auxillary

                outputs = self.model(image.to(self.device))
                outputs = denormalize(outputs, self.mean, self.std)
                if self.stitch:
                    (
                        overlap_crop_coords,
                        tile_pixel_coords,
                    ) = calculate_tile_cropping_coords(
                        tile_level_coords,
                        all_tiles_shape,
                        self.cfg.prediction.overlap,
                        image_shape,
                        self.cfg.prediction.data.patch_size,
                    )
                    predicted_tile = outputs.squeeze()[
                        (*[c for c in overlap_crop_coords], ...)
                    ]
                    tiles.append(predicted_tile.cpu().numpy())
                    stitch_coords = [
                        slice(start, start + end, None)
                        for start, end in zip(tile_pixel_coords, predicted_tile.shape)
                    ]
                    pred[
                        (sample, *[c for c in stitch_coords], ...)
                    ] = predicted_tile.cpu().numpy()
                else:
                    tiles.append(outputs.detach().cpu().numpy())
        self.logger.info("Prediction finished")
        return pred, tiles

    def train_single_epoch(
        self,
        loader: torch.utils.data.DataLoader,
        optimizer: torch.optim.Optimizer,
        scaler: torch.cuda.amp.GradScaler,
        amp: bool,
        max_grad_norm: Optional[float] = None,
    ):
        """_summary_

        _extended_summary_

        Parameters
        ----------
        model : _type_
            _description_
        loader : _type_
            _description_
        """
        avg_loss = MetricTracker()
        self.model.to(self.device)
        self.model.train()

        for image, *auxillary in tqdm(loader):
            optimizer.zero_grad()

            with torch.cuda.amp.autocast(enabled=amp):
                outputs = self.model(image.to(self.device))
            loss = self.loss_func(outputs, *auxillary, self.device)
            scaler.scale(loss).backward()

            if max_grad_norm > 0:
                torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(), max_norm=max_grad_norm
                )
            # TODO fix batches naming
            avg_loss.update(loss.item(), image.shape[0])

            optimizer.step()
        return {"loss": avg_loss.avg}

    def get_loss_function(self):
        return create_loss_function(self.cfg)

    def get_train_dataloader(self) -> DataLoader:
        dataset = create_dataset(self.cfg, "training")
        ##TODO add custom collate function and separate dataloader create function, sampler?
        if not self.cfg.training.running_stats:
            self.logger.info(f"Calculating mean/std of the data")
            dataset.calculate_stats()
        else:
            self.logger.info(f"Using running average of mean/std")
        return (
            DataLoader(
                dataset,
                batch_size=self.cfg.training.data.batch_size,
                num_workers=self.cfg.training.data.num_workers,
            ),
            dataset.mean,
            dataset.std,
        )

    # TODO merge into single dataloader func ?
    def get_val_dataloader(self) -> DataLoader:
        dataset = create_dataset(self.cfg, "evaluation")
        return DataLoader(
            dataset,
            batch_size=self.cfg.evaluation.data.batch_size,
            num_workers=self.cfg.evaluation.data.num_workers,
            pin_memory=True,
        )

    def get_predict_dataloader(self) -> DataLoader:
        # TODO add description
        dataset = create_dataset(self.cfg, "prediction")
        dataset.set_normalization(self.mean, self.std)
        return DataLoader(
            dataset,
            batch_size=self.cfg.prediction.data.batch_size,
            num_workers=self.cfg.prediction.data.num_workers,
            pin_memory=True,
        )

    def get_optimizer_and_scheduler(
        self,
    ) -> Tuple[torch.optim.Optimizer, torch.optim.lr_scheduler._LRScheduler]:
        """Builds a model based on the model_name or load a checkpoint


        _extended_summary_

        Parameters
        ----------
        model_name : _type_
            _description_
        """
        # assert inspect.get
        # TODO call func from factory
        optimizer_name = self.cfg.training.optimizer.name
        optimizer_params = self.cfg.training.optimizer.parameters
        optimizer_func = getattr(torch.optim, optimizer_name)
        # Get the list of all possible parameters of the optimizer
        optim_params = _get_params_from_config(optimizer_func, optimizer_params)
        # TODO add support for different learning rates for different layers
        optimizer = optimizer_func(self.model.parameters(), **optim_params)

        scheduler_name = self.cfg.training.lr_scheduler.name
        scheduler_params = self.cfg.training.lr_scheduler.parameters
        scheduler_func = getattr(torch.optim.lr_scheduler, scheduler_name)
        scheduler_params = _get_params_from_config(scheduler_func, scheduler_params)
        scheduler = scheduler_func(optimizer, **scheduler_params)
        return optimizer, scheduler

    def get_grad_scaler(self) -> torch.cuda.amp.GradScaler:
        toggle = self.cfg.training.amp.toggle
        scaling = self.cfg.training.amp.init_scale
        return torch.cuda.amp.GradScaler(init_scale=scaling, enabled=toggle)

    def save_checkpoint(self, name, save_best):
        """Save the model to a checkpoint file."""
        if save_best:
            torch.save(self.model.state_dict(), "best_checkpoint.pth")
        else:
            torch.save(self.model.state_dict(), "checkpoint.pth")

    # TODO implement proper saving/loading

    def export_model(self, model):
        pass

    def compute_metrics(self, args):
        pass
