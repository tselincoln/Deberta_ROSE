#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2021/10/18 下午5:42
# @Author  : Lan Jiang
# @File    : mytrainer.py

import collections
import inspect
import math
import os
import random
import re
import pdb
import shutil
import sys
import time
from datetime import datetime, timedelta
import warnings
from logging import StreamHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, Dict, List, Optional, Tuple, Union

from tqdm.auto import tqdm

# Integrations must be imported before ML frameworks:
from transformers.integrations import (  # isort: split
    # default_hp_search_backend,
    get_reporting_integration_callbacks,
    hp_params,
    # is_fairscale_available,
    is_optuna_available,
    is_ray_tune_available,
    run_hp_search_optuna,
    run_hp_search_ray,
)
import importlib
def is_fairscale_available():
    return importlib.util.find_spec("fairscale") is not None

import numpy as np
import torch
from packaging import version
from torch import nn
from torch.utils.data.dataloader import DataLoader
from torch.utils.data.dataset import Dataset, IterableDataset
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data.sampler import RandomSampler, SequentialSampler
from torch.nn.functional import kl_div, log_softmax, softmax

from transformers import __version__
from transformers import Trainer
from transformers.debug_utils import DebugOption, DebugUnderflowOverflow
from transformers.deepspeed import deepspeed_init
from transformers.configuration_utils import PretrainedConfig
from transformers.file_utils import (
    CONFIG_NAME,
    WEIGHTS_NAME,
    PushToHubMixin,
    is_apex_available,
    is_datasets_available,
    is_in_notebook,
    is_sagemaker_dp_enabled,
    is_sagemaker_mp_enabled,
    is_torch_tpu_available,
    is_training_run_on_sagemaker,
)
from transformers.trainer_callback import (
    CallbackHandler,
    DefaultFlowCallback,
    PrinterCallback,
    ProgressCallback,
    TrainerCallback,
    TrainerControl,
    TrainerState,
)
from transformers.trainer_pt_utils import (
    DistributedLengthGroupedSampler,
    DistributedSamplerWithLoop,
    DistributedTensorGatherer,
    IterableDatasetShard,
    LabelSmoother,
    LengthGroupedSampler,
    SequentialDistributedSampler,
    ShardSampler,
    distributed_broadcast_scalars,
    distributed_concat,
    find_batch_size,
    get_parameter_names,
    nested_concat,
    nested_detach,
    nested_numpify,
    nested_truncate,
    nested_xla_mesh_reduce,
    reissue_pt_warnings,
)
from transformers.trainer_utils import (
    PREFIX_CHECKPOINT_DIR,
    BestRun,
    EvalLoopOutput,
    EvalPrediction,
    HPSearchBackend,
    PredictionOutput,
    # ShardedDDPOption,
    TrainerMemoryTracker,
    TrainOutput,
    default_compute_objective,
    # default_hp_space,
    denumpify_detensorize,
    get_last_checkpoint,
    number_of_arguments,
    set_seed,
    speed_metrics,
    ExplicitEnum
)

class ShardedDDPOption(ExplicitEnum):
    SIMPLE = "simple"
    ZERO_DP_2 = "zero_dp_2"
    ZERO_DP_3 = "zero_dp_3"
    OFFLOAD = "offload"
    AUTO_WRAP = "auto_wrap"

from transformers.utils import logging

from robustSSANs.utils.optimization import SparseAdamW, get_scheduler

_is_torch_generator_available = False
_is_native_amp_available = False

DEFAULT_CALLBACKS = [DefaultFlowCallback]
DEFAULT_PROGRESS_CALLBACK = ProgressCallback

if is_in_notebook():
    from transformers.utils.notebook import NotebookProgressCallback

    DEFAULT_PROGRESS_CALLBACK = NotebookProgressCallback

if is_apex_available():
    from apex import amp

if version.parse(torch.__version__) >= version.parse("1.6"):
    _is_torch_generator_available = True
    _is_native_amp_available = True
    from torch.cuda.amp import autocast

if is_datasets_available():
    import datasets

if is_torch_tpu_available():
    import torch_xla.core.xla_model as xm
    import torch_xla.debug.metrics as met
    import torch_xla.distributed.parallel_loader as pl

if is_fairscale_available():
    dep_version_check("fairscale")
    import fairscale
    from fairscale.nn.data_parallel import FullyShardedDataParallel as FullyShardedDDP
    from fairscale.nn.data_parallel import ShardedDataParallel as ShardedDDP
    from fairscale.nn.wrap import auto_wrap
    from fairscale.optim import OSS
    from fairscale.optim.grad_scaler import ShardedGradScaler

if is_sagemaker_dp_enabled():
    import smdistributed.dataparallel.torch.distributed as dist
    from smdistributed.dataparallel.torch.parallel.distributed import DistributedDataParallel as DDP
else:
    import torch.distributed as dist

if is_sagemaker_mp_enabled():
    import smdistributed.modelparallel.torch as smp

    from transformers.trainer_pt_utils import smp_forward_backward, smp_forward_only, smp_gather, smp_nested_concat

if is_training_run_on_sagemaker():
    logging.add_handler(StreamHandler(sys.stdout))

if TYPE_CHECKING:
    import optuna

logger = logging.get_logger(__name__)


class AnalysisLoopOutput(NamedTuple):
    predictions: Union[np.ndarray, Tuple[np.ndarray]]
    label_ids: Optional[np.ndarray]
    metrics: Optional[Dict[str, float]]
    num_samples: Optional[int]


class MyTrainer(Trainer):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        args = kwargs['args']

        # Setup Sharded DDP training
        self.sharded_dpp = False
        if args.sharded_ddp:
            if args.local_rank == -1:
                raise ValueError("Using sharded DDP only works in distributed training.")
            elif not is_fairscale_available():
                raise ImportError("Sharded DDP training requires fairscale: `pip install fairscale`.")
            else:
                self.sharded_dpp = True

        # Setup Sharded DDP training
        self.sharded_ddp = None
        if len(args.sharded_ddp) > 0:
            if args.deepspeed:
                raise ValueError(
                    "Using --sharded_ddp xxx together with --deepspeed is not possible, deactivate one of those flags."
                )

            if args.local_rank == -1:
                raise ValueError("Using sharded DDP only works in distributed training.")
            elif not is_fairscale_available():
                raise ImportError("Sharded DDP training requires fairscale: `pip install fairscale`.")
            elif ShardedDDPOption.SIMPLE not in args.sharded_ddp and FullyShardedDDP is None:
                raise ImportError(
                    "Sharded DDP in a mode other than simple training requires fairscale version >= 0.3, found "
                    f"{fairscale.__version__}. Upgrade your fairscale library: `pip install --upgrade fairscale`."
                )
            elif ShardedDDPOption.SIMPLE in args.sharded_ddp:
                self.sharded_ddp = ShardedDDPOption.SIMPLE
            elif ShardedDDPOption.ZERO_DP_2 in args.sharded_ddp:
                self.sharded_ddp = ShardedDDPOption.ZERO_DP_2
            elif ShardedDDPOption.ZERO_DP_3 in args.sharded_ddp:
                self.sharded_ddp = ShardedDDPOption.ZERO_DP_3

        # Mixed precision setup
        self.use_apex = False
        self.use_amp = False
        if args.fp16:
            if args.fp16_backend == "auto":
                backend = "amp" if _is_native_amp_available else "apex"
            else:
                backend = args.fp16_backend

            if backend == "amp":
                self.use_amp = True
                self.scaler = ShardedGradScaler() if self.sharded_dpp else torch.cuda.amp.GradScaler()
            else:
                if not is_apex_available():
                    raise ImportError(
                        "Using FP16 with APEX but APEX is not installed, please refer to https://www.github.com/nvidia/apex."
                    )
                self.use_apex = True

    # Copied from Accelerate.
    def _pad_across_processes(self, tensor, pad_index=-100):
        """
        Recursively pad the tensors in a nested list/tuple/dictionary of tensors from all devices to the same size so
        they can safely be gathered.
        """
        if isinstance(tensor, (list, tuple)):
            return type(tensor)(self._pad_across_processes(t, pad_index=pad_index) for t in tensor)
        elif isinstance(tensor, dict):
            return type(tensor)({k: self._pad_across_processes(v, pad_index=pad_index) for k, v in tensor.items()})
        elif not isinstance(tensor, torch.Tensor):
            raise TypeError(
                f"Can't pad the values of type {type(tensor)}, only of nested list/tuple/dicts of tensors."
            )

        if len(tensor.shape) < 2:
            return tensor
        # Gather all sizes
        size = torch.tensor(tensor.shape, device=tensor.device)[None]
        sizes = self._nested_gather(size).cpu()

        max_size = max(s[1] for s in sizes)
        if tensor.shape[1] == max_size:
            return tensor

        # Then pad to the maximum size
        old_size = tensor.shape
        new_size = list(old_size)
        new_size[1] = max_size
        new_tensor = tensor.new_zeros(tuple(new_size)) + pad_index
        new_tensor[:, : old_size[1]] = tensor
        return new_tensor


    def create_optimizer_and_scheduler(self, num_training_steps: int):
        """
        Setup the optimizer and the learning rate scheduler.

        We provide a reasonable default that works well. If you want to use something else, you can pass a tuple in the
        Trainer's init through :obj:`optimizers`, or subclass and override this method (or :obj:`create_optimizer`
        and/or :obj:`create_scheduler`) in a subclass.
        """
        self.create_optimizer(num_training_steps)
        self.create_scheduler(num_training_steps)

    def create_optimizer(self, num_training_steps: int):
        """
        Setup the optimizer.

        We provide a reasonable default that works well. If you want to use something else, you can pass a tuple in the
        Trainer's init through :obj:`optimizers`, or subclass and override this method in a subclass.
        """
        if self.optimizer is None:
            decay_parameters = get_parameter_names(self.model, [nn.LayerNorm])
            decay_parameters = [name for name in decay_parameters if "bias" not in name]
            optimizer_grouped_parameters = [
                {
                    "params": [p for n, p in self.model.named_parameters() if n in decay_parameters],
                    "params_name": [n for n, p in self.model.named_parameters() if n in decay_parameters],
                    "weight_decay": self.args.weight_decay,
                },
                {
                    "params": [p for n, p in self.model.named_parameters() if n not in decay_parameters],
                    "params_name": [n for n, p in self.model.named_parameters() if n not in decay_parameters],
                    "weight_decay": 0.0,
                },
            ]
            optimizer_cls = SparseAdamW
            optimizer_kwargs = {"betas": (self.args.adam_beta1, self.args.adam_beta2), "eps": self.args.adam_epsilon,
                                "lr": self.args.learning_rate}
            if self.args.do_sparse:
                logger.info(f"***** [Sparse] Lower {self.args.lower} Upper {self.args.upper} *****")
            if self.sharded_ddp == ShardedDDPOption.SIMPLE:
                self.optimizer = OSS(
                    params=optimizer_grouped_parameters,
                    optim=optimizer_cls,
                    **optimizer_kwargs,
                )
            else:
                self.optimizer = optimizer_cls(
                    optimizer_grouped_parameters,
                    do_sparse=self.args.do_sparse,
                    upper=self.args.upper,
                    lower=self.args.lower,
                    sparse_first=self.args.sparse_first,
                    sparse_second=self.args.sparse_second,
                    gamma=self.args.gamma,
                    save_dir=self.args.output_dir,
                    num_warmup_steps=self.args.get_warmup_steps(num_training_steps),
                    **optimizer_kwargs
                )

        if is_sagemaker_mp_enabled():
            self.optimizer = smp.DistributedOptimizer(self.optimizer)

    def create_scheduler(self, num_training_steps: int):
        """
        Setup the scheduler. The optimizer of the trainer must have been set up before this method is called.

        Args:
            num_training_steps (int): The number of training steps to do.
        """
        if self.lr_scheduler is None:
            self.lr_scheduler = get_scheduler(
                self.args.lr_scheduler_type,
                self.optimizer,
                num_warmup_steps=self.args.get_warmup_steps(num_training_steps),
                num_training_steps=num_training_steps,
            )

    def train(
            self,
            resume_from_checkpoint: Optional[Union[str, bool]] = None,
            trial: Union["optuna.Trial", Dict[str, Any]] = None,
            ignore_keys_for_eval: Optional[List[str]] = None,
            **kwargs,
    ):
        """
        Main training entry point.
        Args:
            resume_from_checkpoint (:obj:`str` or :obj:`bool`, `optional`):
                If a :obj:`str`, local path to a saved checkpoint as saved by a previous instance of
                :class:`~transformers.Trainer`. If a :obj:`bool` and equals `True`, load the last checkpoint in
                `args.output_dir` as saved by a previous instance of :class:`~transformers.Trainer`. If present,
                training will resume from the model/optimizer/scheduler states loaded here.
            trial (:obj:`optuna.Trial` or :obj:`Dict[str, Any]`, `optional`):
                The trial run or the hyperparameter dictionary for hyperparameter search.
            ignore_keys_for_eval (:obj:`List[str]`, `optional`)
                A list of keys in the output of your model (if it is a dictionary) that should be ignored when
                gathering predictions for evaluation during the training.
            kwargs:
                Additional keyword arguments used to hide deprecated arguments
        """

        # memory metrics - must set up as early as possible
        self._memory_tracker.start()

        args = self.args

        self.is_in_train = True

        # do_train is not a reliable argument, as it might not be set and .train() still called, so
        # the following is a workaround:
        if args.fp16_full_eval and not args.do_train:
            self.model = self.model.to(args.device)

        if "model_path" in kwargs:
            resume_from_checkpoint = kwargs.pop("model_path")
            warnings.warn(
                "`model_path` is deprecated and will be removed in a future version. Use `resume_from_checkpoint` "
                "instead.",
                FutureWarning,
            )
        if len(kwargs) > 0:
            raise TypeError(f"train() received got unexpected keyword arguments: {', '.join(list(kwargs.keys()))}.")
        # This might change the seed so needs to run first.
        self._hp_search_setup(trial)

        # Model re-init
        model_reloaded = False
        if self.model_init is not None:
            # Seed must be set before instantiating the model when using model_init.
            set_seed(args.seed)
            self.model = self.call_model_init(trial)
            model_reloaded = True
            # Reinitializes optimizer and scheduler
            self.optimizer, self.lr_scheduler = None, None

        # Load potential model checkpoint
        if isinstance(resume_from_checkpoint, bool) and resume_from_checkpoint:
            resume_from_checkpoint = get_last_checkpoint(args.output_dir)
            if resume_from_checkpoint is None:
                raise ValueError(f"No valid checkpoint found in output directory ({args.output_dir})")

        if resume_from_checkpoint is not None:
            if not os.path.isfile(os.path.join(resume_from_checkpoint, WEIGHTS_NAME)):
                raise ValueError(f"Can't find a valid checkpoint at {resume_from_checkpoint}")

            logger.info(f"Loading model from {resume_from_checkpoint}).")

            if os.path.isfile(os.path.join(resume_from_checkpoint, CONFIG_NAME)):
                config = PretrainedConfig.from_json_file(os.path.join(resume_from_checkpoint, CONFIG_NAME))
                checkpoint_version = config.transformers_version
                if checkpoint_version is not None and checkpoint_version != __version__:
                    logger.warn(
                        f"You are resuming training from a checkpoint trained with {checkpoint_version} of "
                        f"Transformers but your current version is {__version__}. This is not recommended and could "
                        "yield to errors or unwanted behaviors."
                    )

            if args.deepspeed:
                # will be resumed in deepspeed_init
                pass
            else:
                # We load the model state dict on the CPU to avoid an OOM error.
                state_dict = torch.load(os.path.join(resume_from_checkpoint, WEIGHTS_NAME), map_location="cpu")
                # If the model is on the GPU, it still works!
                self._load_state_dict_in_model(state_dict)

                # release memory
                del state_dict

        # If model was re-initialized, put it on the right device and update self.model_wrapped
        if model_reloaded:
            if self.place_model_on_device:
                self.model = self.model.to(args.device)
            self.model_wrapped = self.model

        # Keeping track whether we can can len() on the dataset or not
        train_dataset_is_sized = isinstance(self.train_dataset, collections.abc.Sized)

        # Data loader and number of training steps
        train_dataloader = self.get_train_dataloader()

        # Setting up training control variables:
        # number of training epochs: num_train_epochs
        # number of training steps per epoch: num_update_steps_per_epoch
        # total number of training steps to execute: max_steps
        total_train_batch_size = args.train_batch_size * args.gradient_accumulation_steps * args.world_size
        if train_dataset_is_sized:
            num_update_steps_per_epoch = len(train_dataloader) // args.gradient_accumulation_steps
            num_update_steps_per_epoch = max(num_update_steps_per_epoch, 1)
            if args.max_steps > 0:
                max_steps = args.max_steps
                num_train_epochs = args.max_steps // num_update_steps_per_epoch + int(
                    args.max_steps % num_update_steps_per_epoch > 0
                )
                # May be slightly incorrect if the last batch in the training datalaoder has a smaller size but it's
                # the best we can do.
                num_train_samples = args.max_steps * total_train_batch_size
            else:
                max_steps = math.ceil(args.num_train_epochs * num_update_steps_per_epoch)
                num_train_epochs = math.ceil(args.num_train_epochs)
                num_train_samples = len(self.train_dataset) * args.num_train_epochs
        else:
            # see __init__. max_steps is set when the dataset has no __len__
            max_steps = args.max_steps
            # Setting a very large number of epochs so we go as many times as necessary over the iterator.
            num_train_epochs = sys.maxsize
            num_update_steps_per_epoch = max_steps
            num_train_samples = args.max_steps * total_train_batch_size

        if DebugOption.UNDERFLOW_OVERFLOW in self.args.debug:
            if self.args.n_gpu > 1:
                # nn.DataParallel(model) replicates the model, creating new variables and module
                # references registered here no longer work on other gpus, breaking the module
                raise ValueError(
                    "Currently --debug underflow_overflow is not supported under DP. Please use DDP (torch.distributed.launch)."
                )
            else:
                debug_overflow = DebugUnderflowOverflow(self.model)  # noqa

        delay_optimizer_creation = self.sharded_ddp is not None and self.sharded_ddp != ShardedDDPOption.SIMPLE
        if args.deepspeed:
            deepspeed_engine, optimizer, lr_scheduler = deepspeed_init(
                self, num_training_steps=max_steps, resume_from_checkpoint=resume_from_checkpoint
            )
            self.model = deepspeed_engine.module
            self.model_wrapped = deepspeed_engine
            self.deepspeed = deepspeed_engine
            self.optimizer = optimizer
            self.lr_scheduler = lr_scheduler
        elif not delay_optimizer_creation:
            self.create_optimizer_and_scheduler(num_training_steps=max_steps)

        self.state = TrainerState()
        self.state.is_hyper_param_search = trial is not None

        model = self._wrap_model(self.model_wrapped)

        # for the rest of this function `model` is the outside model, whether it was wrapped or not
        if model is not self.model:
            self.model_wrapped = model

        if delay_optimizer_creation:
            self.create_optimizer_and_scheduler(num_training_steps=max_steps)

        # Check if saved optimizer or scheduler states exist
        self._load_optimizer_and_scheduler(resume_from_checkpoint)

        # important: at this point:
        # self.model         is the Transformers Model
        # self.model_wrapped is DDP(Transformers Model), Deepspeed(Transformers Model), etc.

        # Train!
        num_examples = (
            self.num_examples(train_dataloader) if train_dataset_is_sized else total_train_batch_size * args.max_steps
        )

        logger.info("***** Running training *****")
        logger.info(f"  Num examples = {num_examples}")
        logger.info(f"  Num Epochs = {num_train_epochs}")
        logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
        logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_train_batch_size}")
        logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
        logger.info(f"  Total optimization steps = {max_steps}")

        self.state.epoch = 0
        start_time = time.time()
        epochs_trained = 0
        steps_trained_in_current_epoch = 0
        steps_trained_progress_bar = None

        # Check if continuing training from a checkpoint
        if resume_from_checkpoint is not None and os.path.isfile(
                os.path.join(resume_from_checkpoint, "trainer_state.json")
        ):
            self.state = TrainerState.load_from_json(os.path.join(resume_from_checkpoint, "trainer_state.json"))
            epochs_trained = self.state.global_step // num_update_steps_per_epoch
            if not args.ignore_data_skip:
                steps_trained_in_current_epoch = self.state.global_step % (num_update_steps_per_epoch)
                steps_trained_in_current_epoch *= args.gradient_accumulation_steps
            else:
                steps_trained_in_current_epoch = 0

            logger.info("  Continuing training from checkpoint, will skip to saved global_step")
            logger.info(f"  Continuing training from epoch {epochs_trained}")
            logger.info(f"  Continuing training from global step {self.state.global_step}")
            if not args.ignore_data_skip:
                logger.info(
                    f"  Will skip the first {epochs_trained} epochs then the first {steps_trained_in_current_epoch} "
                    "batches in the first epoch. If this takes a lot of time, you can add the `--ignore_data_skip` "
                    "flag to your launch command, but you will resume the training on data already seen by your model."
                )
                if self.is_local_process_zero() and not args.disable_tqdm:
                    steps_trained_progress_bar = tqdm(total=steps_trained_in_current_epoch)
                    steps_trained_progress_bar.set_description("Skipping the first batches")

        # Update the references
        self.callback_handler.model = self.model
        self.callback_handler.optimizer = self.optimizer
        self.callback_handler.lr_scheduler = self.lr_scheduler
        self.callback_handler.train_dataloader = train_dataloader
        self.state.trial_name = self.hp_name(trial) if self.hp_name is not None else None
        self.state.trial_params = hp_params(trial) if trial is not None else None
        # This should be the same if the state has been saved but in case the training arguments changed, it's safer
        # to set this after the load.
        self.state.max_steps = max_steps
        self.state.num_train_epochs = num_train_epochs
        self.state.is_local_process_zero = self.is_local_process_zero()
        self.state.is_world_process_zero = self.is_world_process_zero()

        # tr_loss is a tensor to avoid synchronization of TPUs through .item()
        tr_loss = torch.tensor(0.0).to(args.device)
        # _total_loss_scalar is updated everytime .item() has to be called on tr_loss and stores the sum of all losses
        self._total_loss_scalar = 0.0
        self._globalstep_last_logged = self.state.global_step
        model.zero_grad()

        self.control = self.callback_handler.on_train_begin(args, self.state, self.control)

        # Skip the first epochs_trained epochs to get the random state of the dataloader at the right point.
        if not args.ignore_data_skip:
            for epoch in range(epochs_trained):
                # We just need to begin an iteration to create the randomization of the sampler.
                for _ in train_dataloader:
                    break

        batch_time_list = []
        for epoch in range(epochs_trained, num_train_epochs):
            if isinstance(train_dataloader, DataLoader) and isinstance(train_dataloader.sampler, DistributedSampler):
                train_dataloader.sampler.set_epoch(epoch)
            elif isinstance(train_dataloader.dataset, IterableDatasetShard):
                train_dataloader.dataset.set_epoch(epoch)

            if is_torch_tpu_available():
                parallel_loader = pl.ParallelLoader(train_dataloader, [args.device]).per_device_loader(args.device)
                epoch_iterator = parallel_loader
            else:
                epoch_iterator = train_dataloader

            # Reset the past mems state at the beginning of each epoch if necessary.
            if args.past_index >= 0:
                self._past = None

            steps_in_epoch = (
                len(epoch_iterator) if train_dataset_is_sized else args.max_steps * args.gradient_accumulation_steps
            )
            self.control = self.callback_handler.on_epoch_begin(args, self.state, self.control)

            for step, inputs in enumerate(epoch_iterator):
                # Skip past any already trained steps if resuming training
                if steps_trained_in_current_epoch > 0:
                    steps_trained_in_current_epoch -= 1
                    if steps_trained_progress_bar is not None:
                        steps_trained_progress_bar.update(1)
                    if steps_trained_in_current_epoch == 0:
                        self._load_rng_state(resume_from_checkpoint)
                    continue
                elif steps_trained_progress_bar is not None:
                    steps_trained_progress_bar.close()
                    steps_trained_progress_bar = None

                if step % args.gradient_accumulation_steps == 0:
                    self.control = self.callback_handler.on_step_begin(args, self.state, self.control)
                if (
                        ((step + 1) % args.gradient_accumulation_steps != 0)
                        and args.local_rank != -1
                        and args._no_sync_in_gradient_accumulation
                ):
                    # assert 1 == 0
                    # Avoid unnecessary DDP synchronization since there will be no backward pass on this example.
                    with model.no_sync():
                        ce_loss, kl_loss = self.training_step(model, inputs)
                        if self.use_amp:
                            self.scaler.scale(ce_loss).backward()
                            self.scaler.scale(kl_loss).backward()
                        elif self.use_apex:
                            with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                                scaled_loss.backward()
                        elif self.deepspeed:
                            # loss gets scaled under gradient_accumulation_steps in deepspeed
                            loss = self.deepspeed.backward(loss)
                        else:
                            loss.backward()
                        tr_loss += self.training_step(model, inputs)
                else:
                    ce_loss, kl_loss = self.training_step(model, inputs)

                    if self.args.sparse_first:
                        # if self.args.alpha > 0:
                        #     # combine ce loss and kl loss
                        #     ce_loss += self.args.alpha * kl_loss.detach()
                        # else:
                        # kl loss backward
                        kl_loss.backward(retain_graph=True)
                        kl_loss.detach()
                        # calculate kl mask
                        self.optimizer.calculate_mask(type="kl")
                        self.optimizer.zero_grad()  # do not affect ce loss backward

                    # ce loss backward
                    if self.use_amp:
                        self.scaler.scale(ce_loss).backward()
                    elif self.use_apex:
                        with amp.scale_loss(ce_loss, self.optimizer) as scaled_loss:
                            scaled_loss.backward()
                    elif self.deepspeed:
                        # loss gets scaled under gradient_accumulation_steps in deepspeed
                        ce_loss = self.deepspeed.backward(ce_loss)
                    else:
                        ce_loss.backward()

                    ce_loss.detach()
                    tr_loss += ce_loss
                self.current_flos += float(self.floating_point_ops(inputs))

                # Optimizer step for deepspeed must be called on every step regardless of the value of gradient_accumulation_steps
                if self.deepspeed:
                    self.deepspeed.step()

                if (step + 1) % args.gradient_accumulation_steps == 0 or (
                        # last step in epoch but step is always smaller than gradient_accumulation_steps
                        args.gradient_accumulation_steps >= steps_in_epoch == (step + 1)
                ):
                    # Gradient clipping
                    if args.max_grad_norm is not None and args.max_grad_norm > 0 and not self.deepspeed:
                        # deepspeed does its own clipping

                        if self.use_amp:
                            # AMP: gradients need unscaling
                            self.scaler.unscale_(self.optimizer)

                        if hasattr(self.optimizer, "clip_grad_norm"):
                            # Some optimizers (like the sharded optimizer) have a specific way to do gradient clipping
                            self.optimizer.clip_grad_norm(args.max_grad_norm)
                        elif hasattr(model, "clip_grad_norm_"):
                            # Some models (like FullyShardedDDP) have a specific way to do gradient clipping
                            model.clip_grad_norm_(args.max_grad_norm)
                        else:
                            # Revert to normal clipping otherwise, handling Apex or full precision
                            nn.utils.clip_grad_norm_(
                                amp.master_params(self.optimizer) if self.use_apex else model.parameters(),
                                args.max_grad_norm,
                            )

                    # save gradients
                    # if (self.state.global_step + 1) % 5 == 0:
                    #     output_dir = os.path.join(self.args.output_dir, "grad_dir")
                    #     if not os.path.exists(output_dir):
                    #         os.mkdir(output_dir)
                    #     torch.save({x[0]: x[1].grad.data for x in model.named_parameters()},
                    #                os.path.join(output_dir, f"grad-{step}.pt"))

                    # Optimizer step
                    optimizer_was_run = True
                    if self.deepspeed:
                        pass  # called outside the loop
                    elif self.use_amp:
                        scale_before = self.scaler.get_scale()
                        self.scaler.step(self.optimizer)
                        self.scaler.update()
                        scale_after = self.scaler.get_scale()
                        optimizer_was_run = scale_before <= scale_after
                    else:
                        self.optimizer.step()

                    if optimizer_was_run and not self.deepspeed:
                        self.lr_scheduler.step()

                    model.zero_grad()
                    self.state.global_step += 1
                    self.state.epoch = epoch + (step + 1) / steps_in_epoch
                    self.control = self.callback_handler.on_step_end(args, self.state, self.control)

                    self._maybe_log_save_evaluate(tr_loss, model, trial, epoch, ignore_keys_for_eval)
                    # batch_time_list.append((datetime.now() - batch_start).total_seconds())
                    # print("avg batch time: {}".format(np.mean(batch_time_list)))

                if self.control.should_epoch_stop or self.control.should_training_stop:
                    break

            self.control = self.callback_handler.on_epoch_end(args, self.state, self.control)
            self._maybe_log_save_evaluate(tr_loss, model, trial, epoch, ignore_keys_for_eval)

            if self.control.should_training_stop:
                break

        if args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of training
            delattr(self, "_past")

        logger.info("\n\nTraining completed. Do not forget to share your model on huggingface.co/models =)\n\n")
        if args.load_best_model_at_end and self.state.best_model_checkpoint is not None:
            # Wait for everyone to get here so we are sur the model has been saved by process 0.
            if is_torch_tpu_available():
                xm.rendezvous("load_best_model_at_end")
            elif args.local_rank != -1:
                dist.barrier()

            logger.info(
                f"Loading best model from {self.state.best_model_checkpoint} (score: {self.state.best_metric})."
            )

            best_model_path = os.path.join(self.state.best_model_checkpoint, WEIGHTS_NAME)
            if os.path.exists(best_model_path):
                # We load the model state dict on the CPU to avoid an OOM error.
                state_dict = torch.load(best_model_path, map_location="cpu")
                # If the model is on the GPU, it still works!
                self._load_state_dict_in_model(state_dict)
            else:
                logger.warn(
                    f"Could not locate the best model at {best_model_path}, if you are running a distributed training "
                    "on multiple nodes, you should activate `--save_on_each_node`."
                )

            if self.deepspeed:
                self.deepspeed.load_checkpoint(
                    self.state.best_model_checkpoint, load_optimizer_states=False, load_lr_scheduler_states=False
                )

        # add remaining tr_loss
        self._total_loss_scalar += tr_loss.item()
        train_loss = self._total_loss_scalar / self.state.global_step

        metrics = speed_metrics("train", start_time, num_samples=num_train_samples, num_steps=self.state.max_steps)
        self.store_flos()
        metrics["total_flos"] = self.state.total_flos
        metrics["train_loss"] = train_loss

        self.is_in_train = False

        self._memory_tracker.stop_and_update_metrics(metrics)

        self.log(metrics)

        self.control = self.callback_handler.on_train_end(args, self.state, self.control)

        return TrainOutput(self.state.global_step, train_loss, metrics)

    def training_step(self, model: nn.Module, inputs: Dict[str, Union[torch.Tensor, Any]]):
        """
        Perform a training step on a batch of inputs.
        Subclass and override to inject custom behavior.
        Args:
            model (:obj:`nn.Module`):
                The model to train.
            inputs (:obj:`Dict[str, Union[torch.Tensor, Any]]`):
                The inputs and targets of the model.
                The dictionary will be unpacked before being fed to the model. Most models expect the targets under the
                argument :obj:`labels`. Check your model's documentation for all accepted arguments.
        Return:
            :obj:`torch.Tensor`: The tensor with training loss on this batch.
        """
        model.train()
        inputs = self._prepare_inputs(inputs)

        if is_sagemaker_mp_enabled():
            scaler = self.scaler if self.use_amp else None
            loss_mb = smp_forward_backward(model, inputs, self.args.gradient_accumulation_steps, scaler=scaler)
            return loss_mb.reduce_mean().detach().to(self.args.device)

        if self.use_amp:
            with autocast():
                ce_loss, kl_loss = self.compute_train_loss(model, inputs)
        else:
            ce_loss, kl_loss = self.compute_train_loss(model, inputs)

        if self.args.n_gpu > 1:
            ce_loss = ce_loss.mean()  # mean() to average on multi-gpu parallel training
            kl_loss = kl_loss.mean() if kl_loss is not None else kl_loss  # mean() to average on multi-gpu parallel training

        if self.args.gradient_accumulation_steps > 1 and not self.deepspeed:
            # deepspeed handles loss scaling by gradient_accumulation_steps in its `backward`
            ce_loss = ce_loss / self.args.gradient_accumulation_steps
            kl_loss = kl_loss / self.args.gradient_accumulation_steps if kl_loss is not None else kl_loss

        return (ce_loss, kl_loss)

    def compute_train_loss(self, model, inputs, return_outputs=False):
        """
        How the loss is computed by Trainer. By default, all models return the loss in the first element.
        Subclass and override for custom behavior.
        """
        if self.label_smoother is not None and "labels" in inputs:
            labels = inputs.pop("labels")
        else:
            labels = None
        outputs = model(**inputs)
        # Save past state if it exists
        # TODO: this needs to be fixed and made cleaner later.
        if self.args.past_index >= 0:
            self._past = outputs[self.args.past_index]
        if labels is not None:
            ce_loss = self.label_smoother(outputs, labels)
        else:
            # We don't use .loss here since the model may return tuples instead of ModelOutput.
            if self.args.sparse_first:
                ce_loss = outputs["loss"] if isinstance(outputs, dict) else outputs[0]
                kl_loss = outputs["kl_loss"] if isinstance(outputs, dict) else outputs[-1]
            else:
                ce_loss = outputs["loss"] if isinstance(outputs, dict) else outputs[0]
                kl_loss = None

        return (ce_loss, kl_loss, outputs) if return_outputs else (ce_loss, kl_loss)

    def evaluate(
        self,
        eval_dataset: Optional[Dataset] = None,
        ignore_keys: Optional[List[str]] = None,
        metric_key_prefix: str = "eval",
    ) -> Dict[str, float]:
        """
        Run evaluation and returns metrics.

        The calling script will be responsible for providing a method to compute metrics, as they are task-dependent
        (pass it to the init `compute_metrics` argument).

        You can also subclass and override this method to inject custom behavior.

        Args:
            eval_dataset (`Dataset`, *optional*):
                Pass a dataset if you wish to override `self.eval_dataset`. If it is a [`~datasets.Dataset`], columns
                not accepted by the `model.forward()` method are automatically removed. It must implement the `__len__`
                method.
            ignore_keys (`List[str]`, *optional*):
                A list of keys in the output of your model (if it is a dictionary) that should be ignored when
                gathering predictions.
            metric_key_prefix (`str`, *optional*, defaults to `"eval"`):
                An optional prefix to be used as the metrics key prefix. For example the metrics "bleu" will be named
                "eval_bleu" if the prefix is "eval" (default)

        Returns:
            A dictionary containing the evaluation loss and the potential metrics computed from the predictions. The
            dictionary also contains the epoch number which comes from the training state.
        """
        # memory metrics - must set up as early as possible
        self._memory_tracker.start()

        eval_dataloader = self.get_eval_dataloader(eval_dataset)
        start_time = time.time()

        eval_loop = self.prediction_loop if self.args.use_legacy_prediction_loop else self.evaluation_loop
        output = eval_loop(
            eval_dataloader,
            description="Evaluation",
            # No point gathering the predictions if there are no metrics, otherwise we defer to
            # self.args.prediction_loss_only
            prediction_loss_only=True if self.compute_metrics is None else None,
            ignore_keys=ignore_keys,
            metric_key_prefix=metric_key_prefix,
        )

        total_batch_size = self.args.eval_batch_size * self.args.world_size
        if f"{metric_key_prefix}_jit_compilation_time" in output.metrics:
            start_time += output.metrics[f"{metric_key_prefix}_jit_compilation_time"]
        output.metrics.update(
            speed_metrics(
                metric_key_prefix,
                start_time,
                num_samples=output.num_samples,
                num_steps=math.ceil(output.num_samples / total_batch_size),
            )
        )

        self.log(output.metrics)

        if DebugOption.TPU_METRICS_DEBUG in self.args.debug:
            # tpu-comment: Logging debug metrics for PyTorch/XLA (compile, execute times, ops, etc.)
            xm.master_print(met.metrics_report())

        self.control = self.callback_handler.on_evaluate(self.args, self.state, self.control, output.metrics)

        self._memory_tracker.stop_and_update_metrics(output.metrics)

        return output.metrics

    def evaluation_loop(
            self,
            dataloader: DataLoader,
            description: str,
            prediction_loss_only: Optional[bool] = None,
            ignore_keys: Optional[List[str]] = None,
            metric_key_prefix: str = "eval",
    ) -> EvalLoopOutput:
        """
        Prediction/evaluation loop, shared by :obj:`Trainer.evaluate()` and :obj:`Trainer.predict()`.
        Works both with or without labels.
        """
        prediction_loss_only = (
            prediction_loss_only if prediction_loss_only is not None else self.args.prediction_loss_only
        )

        # if eval is called w/o train init deepspeed here
        if self.args.deepspeed and not self.deepspeed:
            # XXX: eval doesn't have `resume_from_checkpoint` arg but we should be able to do eval
            # from the checkpoint eventually
            deepspeed_engine, _, _ = deepspeed_init(self, num_training_steps=0, resume_from_checkpoint=None)
            self.model = deepspeed_engine.module
            self.model_wrapped = deepspeed_engine
            self.deepspeed = deepspeed_engine
            # XXX: we don't need optim/sched for inference, but this needs to be sorted out, since
            # for example the Z3-optimizer is a must for zero3 to work even for inference - what we
            # don't need is the deepspeed basic optimizer which is self.optimizer.optimizer
            deepspeed_engine.optimizer.optimizer = None
            deepspeed_engine.lr_scheduler = None

        model = self._wrap_model(self.model, training=False)

        # if full fp16 is wanted on eval and this ``evaluation`` or ``predict`` isn't called while
        # ``train`` is running, halve it first and then put on device
        if not self.is_in_train and self.args.fp16_full_eval:
            model = model.half().to(self.args.device)

        batch_size = dataloader.batch_sampler.batch_size

        logger.info(f"***** Running {description} *****")
        if isinstance(dataloader.dataset, collections.abc.Sized):
            logger.info(f"  Num examples = {self.num_examples(dataloader)}")
        else:
            logger.info("  Num examples: Unknown")
        logger.info(f"  Batch size = {batch_size}")

        model.eval()

        self.callback_handler.eval_dataloader = dataloader
        # Do this before wrapping.
        eval_dataset = dataloader.dataset

        if is_torch_tpu_available():
            dataloader = pl.ParallelLoader(dataloader, [self.args.device]).per_device_loader(self.args.device)

        if self.args.past_index >= 0:
            self._past = None

        # Initialize containers
        # losses/preds/labels on GPU/TPU (accumulated for eval_accumulation_steps)
        losses_host = None
        preds_host = None
        labels_host = None
        # losses/preds/labels on CPU (final containers)
        all_losses = None
        all_preds = None
        all_labels = None
        # Will be useful when we have an iterable dataset so don't know its length.

        observed_num_examples = 0
        # Main evaluation loop
        for step, inputs in enumerate(dataloader):
            # Update the observed num examples
            observed_batch_size = find_batch_size(inputs)
            if observed_batch_size is not None:
                observed_num_examples += observed_batch_size

            # Prediction step
            loss, logits, labels = self.prediction_step(model, inputs, prediction_loss_only, ignore_keys=ignore_keys)

            # Update containers on host
            if loss is not None:
                losses = self._nested_gather(loss.repeat(batch_size))
                losses_host = losses if losses_host is None else torch.cat((losses_host, losses), dim=0)
            if logits is not None:
                logits = self._pad_across_processes(logits)
                logits = self._nested_gather(logits)
                preds_host = logits if preds_host is None else nested_concat(preds_host, logits, padding_index=-100)
            if labels is not None:
                labels = self._pad_across_processes(labels)
                labels = self._nested_gather(labels)
                labels_host = labels if labels_host is None else nested_concat(labels_host, labels, padding_index=-100)
            self.control = self.callback_handler.on_prediction_step(self.args, self.state, self.control)

            # Gather all tensors and put them back on the CPU if we have done enough accumulation steps.
            if self.args.eval_accumulation_steps is not None and (step + 1) % self.args.eval_accumulation_steps == 0:
                if losses_host is not None:
                    losses = nested_numpify(losses_host)
                    all_losses = losses if all_losses is None else np.concatenate((all_losses, losses), axis=0)
                if preds_host is not None:
                    logits = nested_numpify(preds_host)
                    all_preds = logits if all_preds is None else nested_concat(all_preds, logits, padding_index=-100)
                if labels_host is not None:
                    labels = nested_numpify(labels_host)
                    all_labels = (
                        labels if all_labels is None else nested_concat(all_labels, labels, padding_index=-100)
                    )

                # Set back to None to begin a new accumulation
                losses_host, preds_host, labels_host = None, None, None

        if self.args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of the evaluation loop
            delattr(self, "_past")

        # Gather all remaining tensors and put them back on the CPU
        if losses_host is not None:
            losses = nested_numpify(losses_host)
            all_losses = losses if all_losses is None else np.concatenate((all_losses, losses), axis=0)
        if preds_host is not None:
            logits = nested_numpify(preds_host)
            all_preds = logits if all_preds is None else nested_concat(all_preds, logits, padding_index=-100)
        if labels_host is not None:
            labels = nested_numpify(labels_host)
            all_labels = labels if all_labels is None else nested_concat(all_labels, labels, padding_index=-100)

        # Number of samples
        if not isinstance(eval_dataset, IterableDataset):
            num_samples = len(eval_dataset)
        # The instance check is weird and does not actually check for the type, but whether the dataset has the right
        # methods. Therefore we need to make sure it also has the attribute.
        elif isinstance(eval_dataset, IterableDatasetShard) and hasattr(eval_dataset, "num_examples"):
            num_samples = eval_dataset.num_examples
        else:
            num_samples = observed_num_examples

        # Number of losses has been rounded to a multiple of batch_size and in a distributed training, the number of
        # samplers has been rounded to a multiple of batch_size, so we truncate.
        if all_losses is not None:
            all_losses = all_losses[:num_samples]
        if all_preds is not None:
            all_preds = nested_truncate(all_preds, num_samples)
        if all_labels is not None:
            all_labels = nested_truncate(all_labels, num_samples)

        # Metrics!
        if self.compute_metrics is not None and all_preds is not None and all_labels is not None:
            metrics = self.compute_metrics(EvalPrediction(predictions=all_preds, label_ids=all_labels))
        else:
            metrics = {}

        # To be JSON-serializable, we need to remove numpy types or zero-d tensors
        metrics = denumpify_detensorize(metrics)

        if all_losses is not None:
            metrics[f"{metric_key_prefix}_loss"] = all_losses.mean().item()

        # Prefix all keys with metric_key_prefix + '_'
        for key in list(metrics.keys()):
            if not key.startswith(f"{metric_key_prefix}_"):
                metrics[f"{metric_key_prefix}_{key}"] = metrics.pop(key)

        return EvalLoopOutput(predictions=all_preds, label_ids=all_labels, metrics=metrics, num_samples=num_samples)

    def prediction_step(
            self,
            model: nn.Module,
            inputs: Dict[str, Union[torch.Tensor, Any]],
            prediction_loss_only: bool,
            ignore_keys: Optional[List[str]] = None,
    ) -> Tuple[Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]]:
        """
        Perform an evaluation step on :obj:`model` using obj:`inputs`.
        Subclass and override to inject custom behavior.
        Args:
            model (:obj:`nn.Module`):
                The model to evaluate.
            inputs (:obj:`Dict[str, Union[torch.Tensor, Any]]`):
                The inputs and targets of the model.
                The dictionary will be unpacked before being fed to the model. Most models expect the targets under the
                argument :obj:`labels`. Check your model's documentation for all accepted arguments.
            prediction_loss_only (:obj:`bool`):
                Whether or not to return the loss only.
            ignore_keys (:obj:`Lst[str]`, `optional`):
                A list of keys in the output of your model (if it is a dictionary) that should be ignored when
                gathering predictions.
        Return:
            Tuple[Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]]: A tuple with the loss,
            logits and labels (each being optional).
        """
        has_labels = all(inputs.get(k) is not None for k in self.label_names)
        inputs = self._prepare_inputs(inputs)
        if ignore_keys is None:
            if hasattr(self.model, "config"):
                ignore_keys = getattr(self.model.config, "keys_to_ignore_at_inference", [])
            else:
                ignore_keys = []

        # labels may be popped when computing the loss (label smoothing for instance) so we grab them first.
        if has_labels:
            labels = nested_detach(tuple(inputs.get(name) for name in self.label_names))
            if len(labels) == 1:
                labels = labels[0]
        else:
            labels = None

        with torch.no_grad():
            if is_sagemaker_mp_enabled():
                raw_outputs = smp_forward_only(model, inputs)
                if has_labels:
                    if isinstance(raw_outputs, dict):
                        loss_mb = raw_outputs["loss"]
                        logits_mb = tuple(v for k, v in raw_outputs.items() if k not in ignore_keys + ["loss"])
                    else:
                        loss_mb = raw_outputs[0]
                        logits_mb = raw_outputs[1:]

                    loss = loss_mb.reduce_mean().detach().cpu()
                    logits = smp_nested_concat(logits_mb)
                else:
                    loss = None
                    if isinstance(raw_outputs, dict):
                        logits_mb = tuple(v for k, v in raw_outputs.items() if k not in ignore_keys)
                    else:
                        logits_mb = raw_outputs
                    logits = smp_nested_concat(logits_mb)
            else:
                if has_labels:
                    loss, outputs = self.compute_loss(model, inputs, return_outputs=True)
                    loss = loss.mean().detach()
                    if isinstance(outputs, dict):
                        logits = tuple(v for k, v in outputs.items() if k not in ignore_keys + ["loss"] + ["kl_loss"])
                    else:
                        logits = outputs[1:]
                else:
                    loss = None
                    if self.use_amp:
                        with autocast():
                            outputs = model(**inputs)
                    else:
                        outputs = model(**inputs)
                    if isinstance(outputs, dict):
                        logits = tuple(v for k, v in outputs.items() if k not in ignore_keys)
                    else:
                        logits = outputs
                    # TODO: this needs to be fixed and made cleaner later.
                    if self.args.past_index >= 0:
                        self._past = outputs[self.args.past_index - 1]

        if prediction_loss_only:
            return (loss, None, None)

        logits = nested_detach(logits)
        if len(logits) == 1:
            logits = logits[0]

        return (loss, logits, labels)

    def analysis(
            self,
            eval_dataset: Optional[Dataset] = None,
            ignore_keys: Optional[List[str]] = None,
            metric_key_prefix: str = "eval",
    ) -> Dict[str, float]:
        """
        Run evaluation and returns metrics.

        The calling script will be responsible for providing a method to compute metrics, as they are task-dependent
        (pass it to the init :obj:`compute_metrics` argument).

        You can also subclass and override this method to inject custom behavior.

        Args:
            eval_dataset (:obj:`Dataset`, `optional`):
                Pass a dataset if you wish to override :obj:`self.eval_dataset`. If it is an :obj:`datasets.Dataset`,
                columns not accepted by the ``model.forward()`` method are automatically removed. It must implement the
                :obj:`__len__` method.
            ignore_keys (:obj:`Lst[str]`, `optional`):
                A list of keys in the output of your model (if it is a dictionary) that should be ignored when
                gathering predictions.
            metric_key_prefix (:obj:`str`, `optional`, defaults to :obj:`"eval"`):
                An optional prefix to be used as the metrics key prefix. For example the metrics "bleu" will be named
                "eval_bleu" if the prefix is "eval" (default)

        Returns:
            A dictionary containing the evaluation loss and the potential metrics computed from the predictions. The
            dictionary also contains the epoch number which comes from the training state.
        """
        # memory metrics - must set up as early as possible
        self._memory_tracker.start()

        eval_dataloader = self.get_eval_dataloader(eval_dataset)
        start_time = time.time()

        eval_loop = self.prediction_loop if self.args.use_legacy_prediction_loop else self.evaluation_loop
        output = eval_loop(
            eval_dataloader,
            description="Evaluation",
            # No point gathering the predictions if there are no metrics, otherwise we defer to
            # self.args.prediction_loss_only
            prediction_loss_only=True if self.compute_metrics is None else None,
            ignore_keys=ignore_keys,
            metric_key_prefix=metric_key_prefix,
        )

        # self.control = self.callback_handler.on_evaluate(self.args, self.state, self.control, output)
        #
        # self._memory_tracker.stop_and_update_metrics(output)

        return output

    def _save_checkpoint(self, model, trial, metrics=None):
        # In all cases, including ddp/dp/deepspeed, self.model is always a reference to the model we
        # want to save except FullyShardedDDP.
        # assert unwrap_model(model) is self.model, "internal model should be a reference to self.model"

        # Save model checkpoint
        checkpoint_folder = f"{PREFIX_CHECKPOINT_DIR}-{self.state.global_step}"

        if self.hp_search_backend is not None and trial is not None:
            if self.hp_search_backend == HPSearchBackend.OPTUNA:
                run_id = trial.number
            else:
                from ray import tune

                run_id = tune.get_trial_id()
            run_name = self.hp_name(trial) if self.hp_name is not None else f"run-{run_id}"
            run_dir = os.path.join(self.args.output_dir, run_name)
        else:
            run_dir = self.args.output_dir
            self.store_flos()

        output_dir = os.path.join(run_dir, checkpoint_folder)
        self.save_model(output_dir)
        if self.deepspeed:
            # under zero3 model file itself doesn't get saved since it's bogus! Unless deepspeed
            # config `stage3_gather_fp16_weights_on_model_save` is True
            self.deepspeed.save_checkpoint(output_dir)

        # Save optimizer and scheduler
        if self.sharded_ddp == ShardedDDPOption.SIMPLE:
            self.optimizer.consolidate_state_dict()

        if is_torch_tpu_available():
            xm.rendezvous("saving_optimizer_states")
            xm.save(self.optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt"))
            with warnings.catch_warnings(record=True) as caught_warnings:
                xm.save(self.lr_scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt"))
                reissue_pt_warnings(caught_warnings)
        elif is_sagemaker_mp_enabled():
            if smp.dp_rank() == 0:
                # Consolidate the state dict on all processed of dp_rank 0
                opt_state_dict = self.optimizer.state_dict()
                # Save it and the scheduler on the main process
                if self.args.should_save:
                    torch.save(opt_state_dict, os.path.join(output_dir, "optimizer.pt"))
                    with warnings.catch_warnings(record=True) as caught_warnings:
                        torch.save(self.lr_scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt"))
                    reissue_pt_warnings(caught_warnings)
                    if self.use_amp:
                        torch.save(self.scaler.state_dict(), os.path.join(output_dir, "scaler.pt"))
        elif self.args.should_save and not self.deepspeed:
            # deepspeed.save_checkpoint above saves model/optim/sched
            torch.save(self.optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt"))
            with warnings.catch_warnings(record=True) as caught_warnings:
                torch.save(self.lr_scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt"))
            reissue_pt_warnings(caught_warnings)
            if self.use_amp:
                torch.save(self.scaler.state_dict(), os.path.join(output_dir, "scaler.pt"))

        # Determine the new best metric / best model checkpoint
        if metrics is not None and self.args.metric_for_best_model is not None:
            metric_to_check = self.args.metric_for_best_model
            if not metric_to_check.startswith("eval_"):
                metric_to_check = f"eval_{metric_to_check}"
            metric_value = metrics[metric_to_check]

            operator = np.greater if self.args.greater_is_better else np.less
            if (
                    self.state.best_metric is None
                    or self.state.best_model_checkpoint is None
                    or operator(metric_value, self.state.best_metric)
            ):
                self.state.best_metric = metric_value
                self.state.best_model_checkpoint = output_dir

        # Save the Trainer state
        if self.args.should_save:
            self.state.save_to_json(os.path.join(output_dir, "trainer_state.json"))

        # Save RNG state in non-distributed training
        rng_states = {
            "python": random.getstate(),
            "numpy": np.random.get_state(),
            "cpu": torch.random.get_rng_state(),
        }
        if torch.cuda.is_available():
            if self.args.local_rank == -1:
                # In non distributed, we save the global CUDA RNG state (will take care of DataParallel)
                rng_states["cuda"] = torch.cuda.random.get_rng_state_all()
            else:
                rng_states["cuda"] = torch.cuda.random.get_rng_state()

        if is_torch_tpu_available():
            rng_states["xla"] = xm.get_rng_state()

        # A process can arrive here before the process 0 has a chance to save the model, in which case output_dir may
        # not yet exist.
        os.makedirs(output_dir, exist_ok=True)
        local_rank = xm.get_local_ordinal() if is_torch_tpu_available() else self.args.local_rank
        if local_rank == -1:
            torch.save(rng_states, os.path.join(output_dir, "rng_state.pth"))
        else:
            torch.save(rng_states, os.path.join(output_dir, f"rng_state_{local_rank}.pth"))

        # Maybe delete some older checkpoints.
        if self.args.should_save:
            self._rotate_checkpoints(use_mtime=True, output_dir=run_dir)