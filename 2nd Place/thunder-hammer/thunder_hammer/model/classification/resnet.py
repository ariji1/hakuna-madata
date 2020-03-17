"""PyTorch ResNet

This started as a copy of https://github.com/pytorch/vision 'resnet.py' (BSD-3-Clause) with
additional dropout and dynamic global avg/max pool.

ResNeXt, SE-ResNeXt, SENet, and MXNet Gluon stem/downsample variants added by Ross Wightman
"""
import math

import math
import logging
from copy import deepcopy
from collections import OrderedDict
from functools import wraps, partial

import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.models.utils import load_state_dict_from_url
from enum import Enum
from pytorch_tools.modules import BasicBlock, Bottleneck, SEModule
from pytorch_tools.modules import GlobalPool2d, BlurPool
from pytorch_tools.modules.residual import conv1x1, conv3x3
# from pytorch_tools.modules import bn_from_name
# from pytorch_tools.modules import activation_from_name
from pytorch_tools.utils.misc import add_docs_for
from pytorch_tools.utils.misc import DEFAULT_IMAGENET_SETTINGS

# avoid overwriting doc string
wraps = partial(wraps, assigned=("__module__", "__name__", "__qualname__", "__annotations__"))


import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import init
from torch.nn.parameter import Parameter

#### Helpfull functions ####
def identity(x, *args, **kwargs):
    return x


class ACT(Enum):
    # Activation names
    CELU = "celu"
    ELU = "elu"
    GLU = "glu"
    IDENTITY = "identity"
    LEAKY_RELU = "leaky_relu"
    MISH = "mish"
    MISH_NAIVE = "mish_naive"
    NONE = "none"
    PRELU = "prelu"
    RELU = "relu"
    RELU6 = "relu6"
    SELU = "selu"
    SWISH = "swish"
    SWISH_NAIVE = "swish_naive"


ACT_FUNC_DICT = {
    ACT.CELU: F.celu,
    ACT.ELU: F.elu,
    ACT.GLU: F.elu,
    ACT.IDENTITY: identity,
    ACT.LEAKY_RELU: F.leaky_relu,
    # ACT.MISH: mish,
    # ACT.MISH_NAIVE: mish_naive,
    # ACT.NONE: identity,
    ACT.PRELU: F.prelu,
    ACT.RELU: F.relu,
    ACT.RELU6: F.relu6,
    ACT.SELU: F.selu,
    # ACT.SWISH: swish,
    # ACT.SWISH_NAIVE: swish_naive,
}


class ABN(nn.Module):
    """Activated Batch Normalization
    This gathers a BatchNorm and an activation function in a single module
    Parameters
    ----------
    num_features : int
        Number of feature channels in the input and output.
    eps : float
        Small constant to prevent numerical issues.
    momentum : float
        Momentum factor applied to compute running statistics.
    affine : bool
        If `True` apply learned scale and shift transformation after normalization.
    activation : str
        Name of the activation functions, one of: `relu`, `leaky_relu`, `elu` or `identity`.
    activation_param : float
        Negative slope for the `leaky_relu` activation.
    """

    def __init__(
        self,
        num_features,
        eps=1e-5,
        momentum=0.1,
        affine=True,
        activation="leaky_relu",
        activation_param=0.01,
    ):
        super(ABN, self).__init__()
        self.num_features = num_features
        self.affine = affine
        self.eps = eps
        self.momentum = momentum
        self.activation = ACT(activation)
        self.activation_param = activation_param
        if self.affine:
            self.weight = nn.Parameter(torch.ones(num_features))
            self.bias = nn.Parameter(torch.zeros(num_features))
        else:
            self.register_parameter("weight", None)
            self.register_parameter("bias", None)
        self.register_buffer("running_mean", torch.zeros(num_features))
        self.register_buffer("running_var", torch.ones(num_features))
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.constant_(self.running_mean, 0)
        nn.init.constant_(self.running_var, 1)
        if self.affine:
            nn.init.constant_(self.weight, 1)
            nn.init.constant_(self.bias, 0)

    def forward(self, x):
        x = F.batch_norm(
            x,
            self.running_mean,
            self.running_var,
            self.weight,
            self.bias,
            self.training,
            self.momentum,
            self.eps,
        )
        func = ACT_FUNC_DICT[self.activation]
        if self.activation == ACT.LEAKY_RELU:
            return func(x, inplace=True, negative_slope=self.activation_param)
        elif self.activation == ACT.ELU:
            return func(x, inplace=True, alpha=self.activation_param)
        else:
            return func(x, inplace=True)

    def _load_from_state_dict(
        self, state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs
    ):
        # Post-Pytorch 1.0 models using standard BatchNorm have a "num_batches_tracked" parameter that we need to ignore
        num_batches_tracked_key = prefix + "num_batches_tracked"
        if num_batches_tracked_key in state_dict:
            del state_dict[num_batches_tracked_key]

        super(ABN, self)._load_from_state_dict(
            state_dict, prefix, local_metadata, strict, missing_keys, error_msgs, unexpected_keys
        )

    def extra_repr(self):
        rep = "{num_features}, eps={eps}, momentum={momentum}, affine={affine}, activation={activation}"
        if self.activation in ["leaky_relu", "elu"]:
            rep += "[{activation_param}]"
        return rep.format(**self.__dict__)



def bn_from_name(norm_name):
    norm_name = norm_name.lower()
    if norm_name == "abn":
        return ABN
    elif norm_name == "inplaceabn" or "inplace_abn":
        return InPlaceABN
    elif norm_name == "inplaceabnsync":
        return InPlaceABNSync
    else:
        raise ValueError(f"Normalization {norm_name} not supported")


class ResNet(nn.Module):
    """ResNet / ResNeXt / SE-ResNeXt / SE-Net

    This class implements all variants of ResNet, ResNeXt and SE-ResNeXt that
      * have > 1 stride in the 3x3 conv layer of bottleneck
      * have conv-bn-act ordering

    This ResNet impl supports a number of stem and downsample options based on 'Bag of Tricks' paper:
    https://arxiv.org/pdf/1812.01187.


    Args:
        block (Block):
            Class for the residual block. Options are BasicBlock, Bottleneck.
        layers (List[int]):
            Numbers of layers in each block.
        pretrained (str, optional):
            If not, returns a model pre-trained on 'str' dataset. `imagenet` is available
            for every model but some have more. Check the code to find out.
        num_classes (int):
            Number of classification classes. Defaults to 1000.
        in_channels (int):
            Number of input (color) channels. Defaults to 3.
        use_se (bool):
            Enable Squeeze-Excitation module in blocks.
        groups (int):
            Number of convolution groups for 3x3 conv in Bottleneck. Defaults to 1.
        base_width (int):
            Factor determining bottleneck channels. `planes * base_width / 64 * groups`. Defaults to 64.
        deep_stem (bool):
            Whether to replace the 7x7 conv1 with 3 3x3 convolution layers. Defaults to False.
        dilated (bool):
            Applying dilation strategy to pretrained ResNet yielding a stride-8 model,
            typically used in Semantic Segmentation. Defaults to False.
        norm_layer (str):
            Nomaliztion layer to use. One of 'abn', 'inplaceabn'. The inplace version lowers memory footprint.
            But increases backward time. Defaults to 'abn'.
        norm_act (str):
            Activation for normalizion layer. It's reccomended to use `relu` with `abn`.
        antialias (bool):
            Flag to turn on Rect-2 antialiasing from https://arxiv.org/abs/1904.11486. Defaults to False.
        encoder (bool):
            Flag to overwrite forward pass to return 5 tensors with different resolutions. Defaults to False.
        drop_rate (float):
            Dropout probability before classifier, for training. Defaults to 0.0.
        global_pool (str):
            Global pooling type. One of 'avg', 'max', 'avgmax', 'catavgmax'. Defaults to 'avg'.
        init_bn0 (bool):
            Zero-initialize the last BN in each residual branch, so that the residual
            branch starts with zeros, and each residual block behaves like an identity.
            This improves the model by 0.2~0.3% according to https://arxiv.org/abs/1706.02677. Defaults to True.

    """

    def __init__(
        self,
        block=None,
        layers=None,
        pretrained=None,  # not used. here for proper signature
        num_classes=1000,
        in_channels=3,
        use_se=False,
        groups=1,
        base_width=64,
        deep_stem=False,
        dilated=False,
        norm_layer="abn",
        norm_act="relu",
        antialias=False,
        encoder=False,
        drop_rate=0.0,
        global_pool="avg",
        init_bn0=True,
    ):

        stem_width = 64
        if norm_layer.lower() == "abn":
            norm_act = "relu"

        norm_layer = bn_from_name(norm_layer)
        self.inplanes = stem_width
        self.num_classes = num_classes
        self.groups = groups
        self.base_width = base_width
        self.drop_rate = drop_rate
        self.block = block
        self.expansion = block.expansion
        self.dilated = dilated
        self.norm_act = norm_act
        super(ResNet, self).__init__()

        if deep_stem:
            self.conv1 = nn.Sequential(
                conv3x3(in_channels, stem_width // 2, 2),
                norm_layer(stem_width // 2, activation=norm_act),
                conv3x3(stem_width // 2, stem_width // 2, 2),
                norm_layer(stem_width // 2, activation=norm_act),
                conv3x3(stem_width // 2, stem_width),
            )
        else:
            self.conv1 = nn.Conv2d(in_channels, stem_width, kernel_size=7, stride=2, padding=3, bias=False)
        self.bn1 = norm_layer(stem_width, activation=norm_act)
        if deep_stem:
            self.maxpool = nn.Sequential()  # don't need it
        elif antialias:
            self.maxpool = nn.Sequential(nn.MaxPool2d(kernel_size=3, stride=1, padding=1), BlurPool())
        else:
            # for se resnets fist maxpool is slightly different
            self.maxpool = nn.MaxPool2d(
                kernel_size=3, stride=2, padding=0 if use_se else 1, ceil_mode=True if use_se else False
            )
        # Output stride is 8 with dilated and 32 without
        stride_3_4 = 1 if self.dilated else 2
        dilation_3 = 2 if self.dilated else 1
        dilation_4 = 4 if self.dilated else 1
        largs = dict(use_se=use_se, norm_layer=norm_layer, norm_act=norm_act, antialias=antialias)
        self.layer1 = self._make_layer(64, layers[0], stride=1, **largs)
        self.layer2 = self._make_layer(128, layers[1], stride=2, **largs)
        self.layer3 = self._make_layer(256, layers[2], stride=stride_3_4, dilation=dilation_3, **largs)
        self.layer4 = self._make_layer(512, layers[3], stride=stride_3_4, dilation=dilation_4, **largs)
        self.global_pool = GlobalPool2d(global_pool)
        self.num_features = 512 * self.expansion
        self.encoder = encoder
        if not encoder:
            self.last_linear = nn.Linear(self.num_features * self.global_pool.feat_mult(), num_classes)
        else:
            self.forward = self.encoder_features

        self._initialize_weights(init_bn0)

    def _make_layer(
        self, planes, blocks, stride=1, dilation=1, use_se=None, norm_layer=None, norm_act=None, antialias=None
    ):
        downsample = None

        if stride != 1 or self.inplanes != planes * self.expansion:
            downsample_layers = []
            if antialias and stride == 2:  # using OrderedDict to preserve ordering and allow loading
                downsample_layers += [("blur", BlurPool())]
            downsample_layers += [
                ("0", conv1x1(self.inplanes, planes * self.expansion, stride=1 if antialias else stride)),
                ("1", norm_layer(planes * self.expansion, activation="identity")),
            ]
            downsample = nn.Sequential(OrderedDict(downsample_layers))

        layers = [
            self.block(
                self.inplanes,
                planes,
                stride,
                downsample,
                self.groups,
                self.base_width,
                use_se,
                dilation,
                norm_layer,
                norm_act,
                antialias,
            )
        ]

        self.inplanes = planes * self.expansion
        for _ in range(1, blocks):
            layers.append(
                self.block(
                    self.inplanes,
                    planes,
                    1,
                    None,
                    self.groups,
                    self.base_width,
                    use_se,
                    dilation,
                    norm_layer,
                    norm_act,
                    antialias,
                )
            )
        return nn.Sequential(*layers)

    def _initialize_weights(self, init_bn0=False):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_in", nonlinearity="relu")
                # nn.init.xavier_uniform_(m.weight)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1.0)
                nn.init.constant_(m.bias, 0.0)
        if init_bn0:
            for m in self.modules():
                if isinstance(m, Bottleneck):
                    nn.init.constant_(m.bn3.weight, 0)
                elif isinstance(m, BasicBlock):
                    nn.init.constant_(m.bn2.weight, 0)

    def encoder_features(self, x):
        x0 = self.conv1(x)
        x0 = self.bn1(x0)
        x1 = self.maxpool(x0)
        x1 = self.layer1(x1)
        x2 = self.layer2(x1)
        x3 = self.layer3(x2)
        x4 = self.layer4(x3)
        return [x4, x3, x2, x1, x0]

    def features(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.maxpool(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        return x

    def logits(self, x):
        x = self.global_pool(x)
        x = torch.flatten(x, 1)
        if self.drop_rate > 0.0:
            x = F.dropout(x, p=self.drop_rate, training=self.training)
        x = self.last_linear(x)
        return x

    def forward(self, x):
        x = self.features(x)
        x = self.logits(x)
        return x

    def load_state_dict(self, state_dict, **kwargs):
        keys = list(state_dict.keys())
        # filter classifier and num_batches_tracked
        for k in keys:
            if (k.startswith("fc") or k.startswith("last_linear")) and self.encoder:
                state_dict.pop(k)
            elif k.startswith("fc"):
                state_dict[k.replace("fc", "last_linear")] = state_dict.pop(k)
            if k.startswith("layer0"):
                state_dict[k.replace("layer0.", "")] = state_dict.pop(k)
        super().load_state_dict(state_dict, **kwargs)

    def freeze(self, num_stages=0):
        if num_stages >= 1:
            for m in [self.conv1, self.bn1, self.maxpool]:
                m.eval()
                for param in m.parameters():
                    param.requires_grad = False
            # layers = [self.layer1, self.layer2, self.layer3, self.layer4]
            # for m in layers[:num_stages-1]:
            #     m.eval()
            #     for param in m.parameters():
            #         param.requires_grad = False
            for i in range(1, num_stages + 1):
                m = getattr(self, "layer{}".format(i))
                m.eval()
                for param in m.parameters():
                    param.requires_grad = False


CFGS = {
    # RESNET MODELS
    "resnet18": {
        "default": {"params": {"block": BasicBlock, "layers": [2, 2, 2, 2]}, **DEFAULT_IMAGENET_SETTINGS},
        "imagenet": {"url": "https://download.pytorch.org/models/resnet18-5c106cde.pth"},
        # EXAMPLE
        # 'imagenet_inplaceabn': {
        #     'params': {'block': BasicBlock, 'layers': [2, 2, 2, 2], 'norm_layer': 'inplaceabn', 'deepstem':True, 'antialias':True},
        #     'url' : 'pathtomodel',
        #     **DEFAULT_IMAGENET_SETTINGS,
        # }
    },
    "resnet34": {
        "default": {"params": {"block": BasicBlock, "layers": [3, 4, 6, 3]}, **DEFAULT_IMAGENET_SETTINGS},
        "imagenet": {"url": "https://download.pytorch.org/models/resnet34-333f7ec4.pth"},  # Acc@1: 71.80. Acc@5: 90.37
        "imagenet2": {  # weigths from rwightman. Acc@1: 73.25. Acc@5: 91.32
            "url": "https://github.com/rwightman/pytorch-image-models/releases/download/v0.1-weights/resnet34-43635321.pth"
        },
        "custom": {"url": "https://download.pytorch.org/models/rn34_aa_76.6.pth"},  # Acc@1: 71.80. Acc@5: 90.37
    },
    "resnet50": {
        "default": {"params": {"block": Bottleneck, "layers": [3, 4, 6, 3]}, **DEFAULT_IMAGENET_SETTINGS},
        "imagenet": {"url": "https://sota.nizhib.ai/models/sws_resnet50-16a12f1b.pth"},
    },
    "resnet101": {
        "default": {"params": {"block": Bottleneck, "layers": [3, 4, 23, 3]}, **DEFAULT_IMAGENET_SETTINGS},
        "imagenet": {"url": "https://download.pytorch.org/models/resnet101-5d3b4d8f.pth"},
    },
    "resnet152": {
        "default": {"params": {"block": Bottleneck, "layers": [3, 8, 36, 3]}, **DEFAULT_IMAGENET_SETTINGS},
        "imagenet": {"url": "https://download.pytorch.org/models/resnet152-b121ed2d.pth"},
    },
    # WIDE RESNET MODELS
    "wide_resnet50_2": {
        "default": {
            "params": {"block": Bottleneck, "layers": [3, 4, 6, 3], "base_width": 128},
            **DEFAULT_IMAGENET_SETTINGS,
        },
        "imagenet": {"url": "https://download.pytorch.org/models/wide_resnet50_2-95faca4d.pth"},
    },
    "wide_resnet101_2": {
        "default": {
            "params": {"block": Bottleneck, "layers": [3, 4, 23, 3], "base_width": 128},
            **DEFAULT_IMAGENET_SETTINGS,
        },
        "imagenet": {"url": "https://download.pytorch.org/models/wide_resnet101_2-32ee1156.pth"},
    },
    # RESNEXT MODELS
    "resnext50_32x4d": {
        "default": {
            "params": {"block": Bottleneck, "layers": [3, 4, 6, 3], "base_width": 4, "groups": 32},
            **DEFAULT_IMAGENET_SETTINGS,
        },
        "imagenet": {  # Acc@1: 75.80. Acc@5: 92.71.
            "url": "https://download.pytorch.org/models/resnext50_32x4d-7cdf4587.pth"
        },
        # weights from rwightman
        "imagenet2": {
            "url": "https://github.com/rwightman/pytorch-image-models/releases/download/v0.1-weights/resnext50d_32x4d-103e99f8.pth"
        },
        "easygold": {"url": "https://sota.nizhib.ai/models/sws_resnext50_32x4-72679e44.pth"},
    },
    "resnext101_32x8d": {
        "default": {
            "params": {"block": Bottleneck, "layers": [3, 4, 23, 3], "base_width": 8, "groups": 32},
            **DEFAULT_IMAGENET_SETTINGS,
        },
        "imagenet": {"url": "https://download.pytorch.org/models/resnext101_32x8d-8ba56ff5.pth"},
        # pretrained on weakly labeled instagram and then tuned on Imagenet
        "imagenet_ig": {"url": "https://download.pytorch.org/models/ig_resnext101_32x8-c38310e5.pth"},
    },
    "resnext101_32x16d": {
        "default": {
            "params": {"block": Bottleneck, "layers": [3, 4, 23, 3], "base_width": 16, "groups": 32},
            **DEFAULT_IMAGENET_SETTINGS,
        },
        # pretrained on weakly labeled instagram and then tuned on Imagenet
        "imagenet_ig": {"url": "https://download.pytorch.org/models/ig_resnext101_32x16-c6f796b0.pth"},
    },
    "resnext101_32x32d": {
        "default": {
            "params": {"block": Bottleneck, "layers": [3, 4, 23, 3], "base_width": 32, "groups": 32},
            **DEFAULT_IMAGENET_SETTINGS,
        },
        # pretrained on weakly labeled instagram and then tuned on Imagenet
        "imagenet_ig": {"url": "https://download.pytorch.org/models/ig_resnext101_32x32-e4b90b00.pth"},
    },
    "resnext101_32x48d": {
        "default": {  # actually it's imagenet_ig. pretrained on weakly labeled instagram and then tuned on Imagenet
            "params": {"block": Bottleneck, "layers": [3, 4, 23, 3], "base_width": 48, "groups": 32},
            **DEFAULT_IMAGENET_SETTINGS,
        },
        "imagenet_ig": {"url": "https://download.pytorch.org/models/ig_resnext101_32x48-3e41cc8a.pth"},
    },
    # SE RESNET MODELS
    "se_resnet50": {
        "default": {
            "params": {"block": Bottleneck, "layers": [3, 4, 6, 3], "use_se": True},
            "url": "http://data.lip6.fr/cadene/pretrainedmodels/se_resnet50-ce0d4300.pth",
            **DEFAULT_IMAGENET_SETTINGS,
        },
        "imagenet": {"url": "http://data.lip6.fr/cadene/pretrainedmodels/se_resnet50-ce0d4300.pth"},
    },
    "se_resnet101": {
        "default": {
            "params": {"block": Bottleneck, "layers": [3, 4, 23, 3], "use_se": True},
            **DEFAULT_IMAGENET_SETTINGS,
        },
        "imagenet": {"url": "http://data.lip6.fr/cadene/pretrainedmodels/se_resnet101-7e38fcc6.pth"},
    },
    "se_resnet152": {
        "default": {
            "params": {"block": Bottleneck, "layers": [3, 4, 36, 3], "use_se": True},
            **DEFAULT_IMAGENET_SETTINGS,
        },
        "imagenet": {"url": "http://data.lip6.fr/cadene/pretrainedmodels/se_resnet152-d17c99b7.pth"},
    },
    # SE RESNEXT MODELS
    "se_resnext50_32x4d": {
        "default": {
            "params": {"block": Bottleneck, "layers": [3, 4, 6, 3], "base_width": 4, "groups": 32, "use_se": True},
            **DEFAULT_IMAGENET_SETTINGS,
        },
        "imagenet": {"url": "http://data.lip6.fr/cadene/pretrainedmodels/se_resnext50_32x4d-a260b3a4.pth"},
    },
    "se_resnext101_32x4d": {
        "default": {
            "params": {"block": Bottleneck, "layers": [3, 4, 23, 3], "base_width": 4, "groups": 32, "use_se": True},
            **DEFAULT_IMAGENET_SETTINGS,
        },
        "imagenet": {"url": "http://data.lip6.fr/cadene/pretrainedmodels/se_resnext101_32x4d-3b2fe3d8.pth"},
    },
}


def _resnet(arch, pretrained=None, **kwargs):
    cfgs = deepcopy(CFGS)
    cfg_settings = cfgs[arch]["default"]
    cfg_params = cfg_settings.pop("params")
    if pretrained:
        pretrained_settings = cfgs[arch][pretrained]
        pretrained_params = pretrained_settings.pop("params", {})
        cfg_settings.update(pretrained_settings)
        cfg_params.update(pretrained_params)
    common_args = set(cfg_params.keys()).intersection(set(kwargs.keys()))
    assert common_args == set(), "Args {} are going to be overwritten by default params for {} weights".format(
        common_args.keys(), pretrained
    )
    kwargs.update(cfg_params)
    model = ResNet(**kwargs)
    if pretrained:
        state_dict = load_state_dict_from_url(cfgs[arch][pretrained]["url"])
        if "state_dict" in state_dict:
            state_dict = state_dict["state_dict"]
            sanitized = {}
            for k, v in state_dict.items():
                sanitized[k.replace("model.", "")] = v

            state_dict = sanitized

        kwargs_cls = kwargs.get("num_classes", None)
        if kwargs_cls and kwargs_cls != cfg_settings["num_classes"]:
            try:
                state_dict["fc.weight"] = torch.cat([state_dict["fc.weight"] * 10])[:kwargs_cls]
                state_dict["fc.bias"] = torch.cat([state_dict["fc.bias"] * 10])[:kwargs_cls]
            except:
                state_dict["last_linear.weight"] = torch.cat([state_dict["last_linear.weight"] * 10])[:kwargs_cls]
                state_dict["last_linear.bias"] = torch.cat([state_dict["last_linear.bias"] * 10])[:kwargs_cls]

        model.load_state_dict(state_dict, strict=True)
    setattr(model, "pretrained_settings", cfg_settings)
    return model


@wraps(ResNet)
@add_docs_for(ResNet)
def resnet18(**kwargs):
    r"""Constructs a ResNet-18 model."""
    return _resnet("resnet18", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def resnet34(**kwargs):
    r"""Constructs a ResNet-34 model."""
    return _resnet("resnet34", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def resnet50(**kwargs):
    r"""Constructs a ResNet-50 model."""
    weights_path = kwargs.get("weights", None)

    if weights_path:
        state_dict = torch.load(weights_path, map_location="cpu")
        kwargs.pop("weights")

    model = _resnet("resnet50", **kwargs)

    if weights_path:
        if "state_dict" in state_dict:
            state_dict = state_dict["state_dict"]
            sanitized = {}
            for k, v in state_dict.items():
                sanitized[k.replace("model.", "")] = v

            state_dict = sanitized
            kwargs_cls = kwargs.get("num_classes", None)
            try:
                state_dict["fc.weight"] = torch.cat([state_dict["fc.weight"] * 10])[:kwargs_cls]
                state_dict["fc.bias"] = torch.cat([state_dict["fc.bias"] * 10])[:kwargs_cls]
            except:
                state_dict["last_linear.weight"] = torch.cat([state_dict["last_linear.weight"] * 10])[:kwargs_cls]
                state_dict["last_linear.bias"] = torch.cat([state_dict["last_linear.bias"] * 10])[:kwargs_cls]

        model.load_state_dict(state_dict, strict=True)

    return model


@wraps(ResNet)
@add_docs_for(ResNet)
def resnet101(**kwargs):
    r"""Constructs a ResNet-101 model."""
    return _resnet("resnet101", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def resnet152(**kwargs):
    """Constructs a ResNet-152 model."""
    return _resnet("resnet152", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def wide_resnet50_2(**kwargs):
    r"""Constructs a Wide ResNet-50-2 model.
    The model is the same as ResNet except for the bottleneck number of channels
    which is twice larger in every block. The number of channels in outer 1x1
    convolutions is the same, e.g. last block in ResNet-50 has 2048-512-2048
    channels, and in Wide ResNet-50-2 has 2048-1024-2048.
    """
    return _resnet("wide_resnet50_2", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def wide_resnet101_2(**kwargs):
    r"""Constructs a Wide ResNet-101-2 model.
    The model is the same as ResNet except for the bottleneck number of channels
    which is twice larger in every block. The number of channels in outer 1x1
    convolutions is the same."""
    return _resnet("wide_resnet101_2", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def resnext50_32x4d(**kwargs):
    r"""Constructs a ResNeXt50-32x4d model."""
    weights_path = kwargs.get("weights", None)

    if weights_path:
        state_dict = torch.load(weights_path, map_location="cpu")
        kwargs.pop("weights")

    model = _resnet("resnext50_32x4d", **kwargs)

    if weights_path:
        if "state_dict" in state_dict:
            state_dict = state_dict["state_dict"]
            sanitized = {}
            for k, v in state_dict.items():
                sanitized[k.replace("model.", "")] = v

            state_dict = sanitized
            kwargs_cls = kwargs.get("num_classes", None)
            try:
                state_dict["fc.weight"] = torch.cat([state_dict["fc.weight"] * 10])[:kwargs_cls]
                state_dict["fc.bias"] = torch.cat([state_dict["fc.bias"] * 10])[:kwargs_cls]
            except:
                state_dict["last_linear.weight"] = torch.cat([state_dict["last_linear.weight"] * 10])[:kwargs_cls]
                state_dict["last_linear.bias"] = torch.cat([state_dict["last_linear.bias"] * 10])[:kwargs_cls]

        model.load_state_dict(state_dict, strict=True)
    return model


@wraps(ResNet)
@add_docs_for(ResNet)
def resnext101_32x8d(**kwargs):
    r"""Constructs a ResNeXt101-32x8d model."""
    return _resnet("resnext101_32x8d", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def ig_resnext101_32x8d(**kwargs):
    r"""Constructs a ResNeXt-101 32x8 model pre-trained on weakly-supervised data
    and finetuned on ImageNet from Figure 5 in
    `"Exploring the Limits of Weakly Supervised Pretraining" <https://arxiv.org/abs/1805.00932>`_
    Weights from https://pytorch.org/hub/facebookresearch_WSL-Images_resnext/"""
    return _resnet("ig_resnext101_32x8d", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def ig_resnext101_32x16d(**kwargs):
    r"""Constructs a ResNeXt-101 32x16 model pre-trained on weakly-supervised data."""
    return _resnet("resnext101_32x16d", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def ig_resnext101_32x32d(**kwargs):
    r"""Constructs a ResNeXt-101 32x32 model pre-trained on weakly-supervised data."""
    return _resnet("ig_resnext101_32x32d", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def ig_resnext101_32x48d(**kwargs):
    r"""Constructs a ResNeXt-101 32x48 model pre-trained on weakly-supervised data."""
    return _resnet("ig_resnext101_32x48d", **kwargs)


# @wraps(ResNet)
# @add_docs_for(ResNet)
# def se_resnet34(**kwargs):
#     """TODO: Add Doc"""
#     return  _resnet('se_resnet34', **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def se_resnet50(**kwargs):
    """TODO: Add Doc"""
    return _resnet("se_resnet50", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def se_resnet101(**kwargs):
    """TODO: Add Doc"""
    return _resnet("se_resnet101", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def se_resnet152(**kwargs):
    """TODO: Add Doc"""
    return _resnet("se_resnet152", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def se_resnext50_32x4d(**kwargs):
    """TODO: Add Doc"""
    return _resnet("se_resnext50_32x4d", **kwargs)


@wraps(ResNet)
@add_docs_for(ResNet)
def se_resnext101_32x4d(**kwargs):
    """TODO: Add Doc"""
    return _resnet("se_resnext101_32x4d", **kwargs)


if __name__ == "__main__":
    net = se_resnext50_32x4d(num_classes=50, pretrained="imagenet")
