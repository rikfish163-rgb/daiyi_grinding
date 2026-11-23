"""Small full-resolution RGB-D segmentation network; optional PyTorch runtime."""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F


class ConvBlock(nn.Module):
    def __init__(self, incoming, outgoing):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Conv2d(incoming, outgoing, 3, padding=1, bias=False),
            nn.GroupNorm(4, outgoing),
            nn.SiLU(),
            nn.Conv2d(outgoing, outgoing, 3, padding=1, bias=False),
            nn.GroupNorm(4, outgoing),
            nn.SiLU(),
        )

    def forward(self, value):
        return self.layers(value)


class DefectUNet(nn.Module):
    """Three classes: background/retained CAD, flash and compact burr."""

    def __init__(self, channels=11, width=16):
        super().__init__()
        self.config = {"channels": channels, "width": width, "classes": 3}
        self.first = ConvBlock(channels, width)
        self.second = ConvBlock(width, width * 2)
        self.third = ConvBlock(width * 2, width * 4)
        self.bottom = ConvBlock(width * 4, width * 6)
        self.up3 = ConvBlock(width * 10, width * 4)
        self.up2 = ConvBlock(width * 6, width * 2)
        self.up1 = ConvBlock(width * 3, width)
        self.head = nn.Conv2d(width, 3, 1)

    def forward(self, value):
        first = self.first(value)
        second = self.second(F.avg_pool2d(first, 2))
        third = self.third(F.avg_pool2d(second, 2))
        bottom = self.bottom(F.avg_pool2d(third, 2))

        def up(low, skip, block):
            resized = F.interpolate(
                low, size=skip.shape[-2:], mode="bilinear", align_corners=False
            )
            return block(torch.cat((resized, skip), dim=1))

        value = up(bottom, third, self.up3)
        value = up(value, second, self.up2)
        value = up(value, first, self.up1)
        return self.head(value)


def segmentation_loss(logits, labels):
    """Weighted CE plus foreground Dice, including normal-only patches."""
    weights = logits.new_tensor([0.5, 2.0, 3.0], dtype=torch.float32)
    cross_entropy = F.cross_entropy(logits.float(), labels, weight=weights)
    probability = logits.float().softmax(dim=1)
    truth = F.one_hot(labels, 3).permute(0, 3, 1, 2).float()
    dimensions = (0, 2, 3)
    intersection = (probability * truth).sum(dim=dimensions)
    denominator = probability.sum(dim=dimensions) + truth.sum(dim=dimensions)
    dice = 1 - ((2 * intersection + 1) / (denominator + 1))[1:].mean()
    return cross_entropy + dice
