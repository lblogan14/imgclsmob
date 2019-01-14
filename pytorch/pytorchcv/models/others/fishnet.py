"""
    FishNet, implemented in PyTorch.
    Original paper: 'FishNet: A Versatile Backbone for Image, Region, and Pixel Level Prediction,'
    http://papers.nips.cc/paper/7356-fishnet-a-versatile-backbone-for-image-region-and-pixel-level-prediction.pdf.
"""

__all__ = ['FishNet', 'fishnet99', 'fishnet150']

import os
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
from pytorch.pytorchcv.models.common import pre_conv1x1_block, pre_conv3x3_block, conv1x1, SesquialteralHourglass
from pytorch.pytorchcv.models.senet import SEInitBlock


def channel_squeeze(x,
                    groups):
    """
    Channel squeeze operation.

    Parameters:
    ----------
    x : Tensor
        Input tensor.
    groups : int
        Number of groups.

    Returns
    -------
    Tensor
        Resulted tensor.
    """
    batch, channels, height, width = x.size()
    channels_per_group = channels // groups
    x = x.view(batch, channels_per_group, groups, height, width).sum(dim=2)
    return x


class ChannelSqueeze(nn.Module):
    """
    Channel squeeze layer. This is a wrapper over the same operation. It is designed to save the number of groups.

    Parameters:
    ----------
    channels : int
        Number of channels.
    groups : int
        Number of groups.
    """
    def __init__(self,
                 channels,
                 groups):
        super(ChannelSqueeze, self).__init__()
        if channels % groups != 0:
            raise ValueError('channels must be divisible by groups')
        self.groups = groups

    def forward(self, x):
        return channel_squeeze(x, self.groups)


class InterpolationBlock(nn.Module):
    """
    Interpolation block.

    Parameters:
    ----------
    scale_factor : float
        Multiplier for spatial size.
    mode : str, default 'nearest'
        Algorithm used for upsampling.
    """
    def __init__(self,
                 scale_factor,
                 mode="nearest"):
        super(InterpolationBlock, self).__init__()
        self.scale_factor = scale_factor
        self.mode = mode

    def forward(self, x):
        return F.interpolate(
            input=x,
            scale_factor=self.scale_factor,
            mode=self.mode,
            align_corners=True)


class FishBottleneck(nn.Module):
    """
    FishNet bottleneck block for residual unit.

    Parameters:
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    stride : int or tuple/list of 2 int
        Strides of the convolution.
    dilation : int or tuple/list of 2 int
        Dilation value for convolution layer.
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 stride,
                 dilation):
        super(FishBottleneck, self).__init__()
        mid_channels = out_channels // 4

        self.conv1 = pre_conv1x1_block(
            in_channels=in_channels,
            out_channels=mid_channels)
        self.conv2 = pre_conv3x3_block(
            in_channels=mid_channels,
            out_channels=mid_channels,
            stride=stride,
            padding=dilation,
            dilation=dilation)
        self.conv3 = pre_conv1x1_block(
            in_channels=mid_channels,
            out_channels=out_channels)

    def forward(self, x):
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.conv3(x)
        return x


class FishBlock(nn.Module):
    """
    FishNet block.

    Parameters:
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    stride : int or tuple/list of 2 int, default 1
        Strides of the convolution.
    dilation : int or tuple/list of 2 int, default 1
        Dilation value for convolution layer.
    squeeze : bool, default False
        Whether to use a channel squeeze operation.
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 stride=1,
                 dilation=1,
                 squeeze=False):
        super(FishBlock, self).__init__()
        self.squeeze = squeeze
        self.resize_identity = (in_channels != out_channels) or (stride != 1)

        self.body = FishBottleneck(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
            dilation=dilation)
        if squeeze:
            assert (in_channels // 2 == out_channels)
            self.c_squeeze = ChannelSqueeze(
                channels=in_channels,
                groups=2)
        elif self.resize_identity:
            self.identity_conv = pre_conv1x1_block(
                in_channels=in_channels,
                out_channels=out_channels,
                stride=stride)

    def forward(self, x):
        if self.squeeze:
            identity = self.c_squeeze(x)
        elif self.resize_identity:
            identity = self.identity_conv(x)
        else:
            identity = x
        x = self.body(x)
        x = x + identity
        return x


class DownUnit(nn.Module):
    """
    FishNet down unit.

    Parameters:
    ----------
    in_channels : int
        Number of input channels.
    out_channels_list : list of int
        Number of output channels for each block.
    """
    def __init__(self,
                 in_channels,
                 out_channels_list):
        super(DownUnit, self).__init__()

        self.blocks = nn.Sequential()
        for i, out_channels in enumerate(out_channels_list):
            self.blocks.add_module("block{}".format(i + 1), FishBlock(
                in_channels=in_channels,
                out_channels=out_channels))
            in_channels = out_channels
        self.blocks.add_module("pool", nn.MaxPool2d(
            kernel_size=2,
            stride=2))

    def forward(self, x):
        x = self.blocks(x)
        return x


class UpUnit(nn.Module):
    """
    FishNet up unit.

    Parameters:
    ----------
    in_channels : int
        Number of input channels.
    out_channels_list : list of int
        Number of output channels for each block.
    """
    def __init__(self,
                 in_channels,
                 out_channels_list):
        super(UpUnit, self).__init__()

        self.blocks = nn.Sequential()
        for i, out_channels in enumerate(out_channels_list):
            self.blocks.add_module("block{}".format(i + 1), FishBlock(
                in_channels=in_channels,
                out_channels=out_channels))
            in_channels = out_channels
        self.blocks.add_module("upsample", InterpolationBlock(scale_factor=2))

    def forward(self, x):
        x = self.blocks(x)
        return x


class SkipUnit(nn.Module):
    """
    FishNet skip connection unit.

    Parameters:
    ----------
    in_channels : int
        Number of input channels.
    out_channels_list : list of int
        Number of output channels for each block.
    """
    def __init__(self,
                 in_channels,
                 out_channels_list):
        super(SkipUnit, self).__init__()

        self.blocks = nn.Sequential()
        for i, out_channels in enumerate(out_channels_list):
            self.blocks.add_module("block{}".format(i + 1), FishBlock(
                in_channels=in_channels,
                out_channels=out_channels))
            in_channels = out_channels

    def forward(self, x):
        x = self.blocks(x)
        return x


class FishNet(nn.Module):
    """
    FishNet model from 'FishNet: A Versatile Backbone for Image, Region, and Pixel Level Prediction,'
    http://papers.nips.cc/paper/7356-fishnet-a-versatile-backbone-for-image-region-and-pixel-level-prediction.pdf.

    Parameters:
    ----------
    direct_channels : list of list of list of int
        Number of output channels for each unit along the straight path.
    skip_channels : list of list of list of int
        Number of output channels for each unit along the straight path.
    init_block_channels : int
        Number of output channels for the initial unit.
    in_channels : int, default 3
        Number of input channels.
    in_size : tuple of two ints, default (224, 224)
        Spatial size of the expected input image.
    num_classes : int, default 1000
        Number of classification classes.
    """
    def __init__(self,
                 direct_channels,
                 skip_channels,
                 init_block_channels,
                 in_channels=3,
                 in_size=(224, 224),
                 num_classes=1000):
        super(FishNet, self).__init__()
        self.in_size = in_size
        self.num_classes = num_classes

        self.features = nn.Sequential()
        self.features.add_module("init_block", SEInitBlock(
            in_channels=in_channels,
            out_channels=init_block_channels))
        in_channels = init_block_channels

        depth = len(direct_channels[0])

        down2_seq = nn.Sequential()
        skip2_seq = nn.Sequential()

        down1_seq = nn.Sequential()
        skip1_seq = nn.Sequential()
        for i in range(depth + 1):
            skip1_channels_per_stage = skip_channels[0][i]
            skip1_seq.add_module("stage{}".format(i + 1), DownUnit(
                in_channels=in_channels,
                out_channels=skip1_channels_per_stage[0],
                layers=len(skip1_channels_per_stage)))
            if i < depth:
                down1_channels_per_stage = direct_channels[0][i]
                down1_seq.add_module("stage{}".format(i + 1), DownUnit(
                    in_channels=in_channels,
                    out_channels=down1_channels_per_stage[0],
                    layers=len(down1_channels_per_stage)))
                in_channels = down1_channels_per_stage[0]
            else:
                in_channels = skip1_channels_per_stage[0]

        up_seq = nn.Sequential()

        self.features.add_module("sesquialteral_hourglass", SesquialteralHourglass(
            down1_seq=down1_seq,
            skip1_seq=skip1_seq,
            up_seq=up_seq,
            down2_seq=down2_seq,
            skip2_seq=skip2_seq))
        self.features.add_module("final_pool", nn.AvgPool2d(
            kernel_size=7,
            stride=1))

        self.output = nn.Sequential()
        self.output.add_module("final_conv", conv1x1(
            in_channels=in_channels,
            out_channels=num_classes))

        self._init_params()

    def _init_params(self):
        for name, module in self.named_modules():
            if isinstance(module, nn.Conv2d):
                init.kaiming_uniform_(module.weight)
                if module.bias is not None:
                    init.constant_(module.bias, 0)

    def forward(self, x):
        x = self.features(x)
        x = self.output(x)
        x = x.view(x.size(0), -1)
        return x


def get_fishnet(blocks,
                model_name=None,
                pretrained=False,
                root=os.path.join('~', '.torch', 'models'),
                **kwargs):
    """
    Create FishNet model with specific parameters.

    Parameters:
    ----------
    blocks : int
        Number of blocks.
    model_name : str or None, default None
        Model name for loading pretrained model.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.mxnet/models'
        Location for keeping the model parameters.
    """

    if blocks == 99:
        raise ValueError("Unsupported FishNet with number of blocks: {}".format(blocks))
    elif blocks == 150:
        direct_layers = [[2, 4, 8], [2, 2, 2], [2, 2, 4]]
        skip_layers = [[2, 2, 2, 2], [2, 2, 2, 2]]
        direct_channels_per_layers = [[128, 256, 512], [512, 512, 384], [256, 320, 832]]
        skip_channels_per_layers = [[64, 128, 256, 512], [64, 128, 256, 512]]
    else:
        raise ValueError("Unsupported FishNet with number of blocks: {}".format(blocks))

    direct_channels = [([cij] * lij for (cij, lij) in zip(ci, li)) for (ci, li) in
                       zip(direct_channels_per_layers, direct_layers)]
    skip_channels = [([cij] * lij for (cij, lij) in zip(ci, li)) for (ci, li) in
                     zip(skip_channels_per_layers, skip_layers)]

    init_block_channels = 64

    net = FishNet(
        direct_channels=direct_channels,
        skip_channels=skip_channels,
        init_block_channels=init_block_channels,
        **kwargs)

    if pretrained:
        if (model_name is None) or (not model_name):
            raise ValueError("Parameter `model_name` should be properly initialized for loading pretrained model.")
        from .model_store import download_model
        download_model(
            net=net,
            model_name=model_name,
            local_model_store_dir_path=root)

    return net


def fishnet99(**kwargs):
    """
    FishNet-99 model from 'FishNet: A Versatile Backbone for Image, Region, and Pixel Level Prediction,'
    http://papers.nips.cc/paper/7356-fishnet-a-versatile-backbone-for-image-region-and-pixel-level-prediction.pdf.

    Parameters:
    ----------
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.torch/models'
        Location for keeping the model parameters.
    """
    return get_fishnet(blocks=99, model_name="fishnet99", **kwargs)


def fishnet150(**kwargs):
    """
    FishNet-150 model from 'FishNet: A Versatile Backbone for Image, Region, and Pixel Level Prediction,'
    http://papers.nips.cc/paper/7356-fishnet-a-versatile-backbone-for-image-region-and-pixel-level-prediction.pdf.

    Parameters:
    ----------
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.torch/models'
        Location for keeping the model parameters.
    """
    return get_fishnet(blocks=150, model_name="fishnet150", **kwargs)


def _calc_width(net):
    import numpy as np
    net_params = filter(lambda p: p.requires_grad, net.parameters())
    weight_count = 0
    for param in net_params:
        weight_count += np.prod(param.size())
    return weight_count


def _test():
    import torch
    from torch.autograd import Variable

    pretrained = False

    models = [
        # fishnet99,
        fishnet150,
    ]

    for model in models:

        net = model(pretrained=pretrained)

        # net.train()
        net.eval()
        weight_count = _calc_width(net)
        print("m={}, {}".format(model.__name__, weight_count))
        assert (model != fishnet99 or weight_count == 16628904)
        assert (model != fishnet150 or weight_count == 24959400)

        x = Variable(torch.randn(1, 3, 224, 224))
        y = net(x)
        assert (tuple(y.size()) == (1, 1000))


if __name__ == "__main__":
    _test()