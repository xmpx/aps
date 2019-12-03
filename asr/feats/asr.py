#!/usr/bin/env python

# wujian@2019
"""
Feature transform for ASR
"""
import math

import torch as th
import torch.nn as nn
import torch.nn.functional as F

from .utils import STFT, EPSILON, init_melfilter, init_dct
from .spec_aug import specaug


class SpectrogramTransform(STFT):
    """
    Compute spectrogram as a layer
    """
    def __init__(self,
                 frame_len,
                 frame_hop,
                 window="hamm",
                 round_pow_of_two=True):
        super(SpectrogramTransform,
              self).__init__(frame_len,
                             frame_hop,
                             window=window,
                             round_pow_of_two=round_pow_of_two)

    def dim(self):
        return self.num_bins

    def len(self, xlen):
        return self.num_frames(xlen)

    def forward(self, x):
        """
        args:
            x: input signal, N x C x S or N x S
        return:
            m: magnitude, N x C x T x F or N x T x F
        """
        m, _ = super().forward(x)
        m = th.transpose(m, -1, -2)
        return m


class AbsTransform(nn.Module):
    """
    Absolute transform
    """
    def __init__(self):
        super(AbsTransform, self).__init__()

    def forward(self, x):
        """
        args:
            x: enhanced complex spectrogram N x T x F
        return:
            y: enhanced N x T x F
        """
        return x.abs()


class MelTransform(nn.Module):
    """
    Mel tranform as a layer
    """
    def __init__(self,
                 frame_len,
                 round_pow_of_two=True,
                 sr=16000,
                 num_mels=80,
                 fmin=0.0,
                 fmax=None):
        super(MelTransform, self).__init__()
        # num_mels x (N // 2 + 1)
        filters = init_melfilter(frame_len,
                                 round_pow_of_two=round_pow_of_two,
                                 sr=sr,
                                 num_mels=num_mels,
                                 fmax=fmax,
                                 fmin=fmin)
        self.num_mels, self.num_bins = filters.shape
        self.filters = nn.Parameter(filters, requires_grad=False)
        self.fmin = fmin
        self.fmax = sr // 2 if fmax is None else fmax

    def dim(self):
        return self.num_mels

    def extra_repr(self):
        return "fmin={0}, fmax={1}, mel_filter={2[0]}x{2[1]}".format(
            self.fmin, self.fmax, self.filters.shape)

    def forward(self, x):
        """
        args:
            x: spectrogram, N x C x T x F or N x T x F
        return:
            f: mel-spectrogram, N x C x T x B
        """
        if x.dim() not in [3, 4]:
            raise RuntimeError("MelTransform expect 3/4D tensor, " +
                               f"but got {x.dim():d} instead")
        # N x T x F => N x T x M
        f = F.linear(x, self.filters, bias=None)
        return f


class LogTransform(nn.Module):
    """
    Transform linear domain to log domain
    """
    def __init__(self, eps=EPSILON):
        super(LogTransform, self).__init__()
        self.eps = eps

    def dim_scale(self):
        return 1

    def extra_repr(self):
        return f"eps={self.eps:f}"

    def forward(self, x):
        """
        args:
            x: features in linear domain, N x C x T x F or N x T x F
        return:
            y: features in log domain, N x C x T x F or N x T x F
        """
        x = th.clamp(x, min=self.eps)
        return th.log(x)


class DiscreteCosineTransform(nn.Module):
    """
    DCT as a layer (for mfcc features)
    """
    def __init__(self, num_ceps=13, num_mels=40, lifter=0):
        super(DiscreteCosineTransform, self).__init__()
        self.lifter = lifter
        self.num_ceps = num_ceps
        self.dct = nn.Parameter(init_dct(num_ceps, num_mels),
                                requires_grad=False)
        cepstral_lifter = 1 + lifter * 0.5 * th.sin(
            math.pi * th.arange(1, 1 + num_ceps) / lifter)
        self.cepstral_lifter = nn.Parameter(cepstral_lifter,
                                            requires_grad=False)

    def dim(self):
        return self.num_ceps

    def extra_repr(self):
        return "cepstral_lifter={0}, dct={1[0]}x{1[1]}".format(
            self.lifter, self.dct.shape)

    def forward(self, x):
        """
        args:
            x: log mel-spectrogram, N x C x T x B
        return:
            f: mfcc, N x C x T x P
        """
        f = F.linear(x, self.dct, bias=None)
        f = f * self.cepstral_lifter
        return f


class CmvnTransform(nn.Module):
    """
    Utterance-level mean-variance normalization
    """
    def __init__(self, norm_mean=True, norm_var=True):
        super(CmvnTransform, self).__init__()
        self.norm_mean = norm_mean
        self.norm_var = norm_var

    def extra_repr(self):
        return f"norm_mean={self.norm_mean}, norm_var={self.norm_var}"

    def dim_scale(self):
        return 1

    def forward(self, x):
        """
        args:
            x: feature without normalized, N x C x T x F or N x T x F
        return:
            y: normalized feature, N x C x T x F or N x T x F
        """
        if not self.norm_mean and not self.norm_var:
            return x
        m = th.mean(x, -1, keepdim=True)
        s = th.std(x, -1, keepdim=True)
        if self.norm_mean:
            x -= m
        if self.norm_var:
            x /= th.clamp(s, min=EPSILON)
        return x


class SpecAugTransform(nn.Module):
    """
    Spectra data augmentation
    """
    def __init__(self,
                 p=0.5,
                 wrap_step=4,
                 mask_band=30,
                 mask_step=40,
                 num_bands=2,
                 num_steps=2):
        super(SpecAugTransform, self).__init__()
        self.num_bands, self.num_steps = num_bands, num_steps
        self.W, self.F, self.T = wrap_step, mask_band, mask_step
        self.p = p

    def extra_repr(self):
        return f"time_wrap={self.W}, max_band={self.F}, max_step={self.T}, p={self.p}, " \
                + f"num_bands={self.num_bands}, num_steps={self.num_steps}"

    def forward(self, x):
        """
        args:
            x: original features, N x C x T x F or N x T x F
        return:
            y: augmented features
        """
        if self.training and th.rand(1).item() < self.p:
            if x.dim() == 4:
                raise RuntimeError("Not supported for multi-channel")
            aug = []
            for n in range(x.shape[0]):
                aug.append(
                    specaug(x[n],
                            W=self.W,
                            F=self.F,
                            T=self.T,
                            num_freq_masks=self.num_bands,
                            num_time_masks=self.num_steps,
                            replace_with_zero=True))
            x = th.stack(aug, 0)
        return x


class SpliceTransform(nn.Module):
    """
    Do splicing as well as downsampling if needed
    """
    def __init__(self, lctx=0, rctx=0, ds_rate=1):
        super(SpliceTransform, self).__init__()
        self.rate = ds_rate
        self.lctx = max(lctx, 0)
        self.rctx = max(rctx, 0)

    def extra_repr(self):
        return f"context=({self.lctx}, {self.rctx}), downsample_rate={self.rate}"

    def dim_scale(self):
        return (1 + self.rctx + self.lctx)

    def forward(self, x):
        """
        args:
            x: original feature, N x ... x Ti x F
        return:
            y: spliced feature, N x ... x To x FD
        """
        T = x.shape[-2]
        T = T - T % self.rate
        if self.lctx + self.rctx != 0:
            ctx = []
            for c in range(-self.lctx, self.rctx + 1):
                idx = th.arange(c, c + T, device=x.device, dtype=th.int64)
                idx = th.clamp(idx, min=0, max=T - 1)
                # N x ... x T x F
                ctx.append(th.index_select(x, -2, idx))
            # N x ... x T x FD
            x = th.cat(ctx, -1)
        if self.rate != 1:
            x = x[..., ::self.rate, :]
        return x


class DeltaTransform(nn.Module):
    """
    Add delta features
    """
    def __init__(self, ctx=2, order=2):
        super(DeltaTransform, self).__init__()
        self.ctx = ctx
        self.order = order

    def extra_repr(self):
        return f"context={self.ctx}, order={self.order}"

    def dim_scale(self):
        return self.order

    def _add_delta(self, x):
        dx = th.zeros_like(x)
        for i in range(1, self.ctx + 1):
            dx[..., :-i, :] += i * x[..., i:, :]
            dx[..., i:, :] += -i * x[..., :-i, :]
            dx[..., -i:, :] += i * x[..., -1:, :]
            dx[..., :i, :] += -i * x[..., :1, :]
        dx /= 2 * sum(i**2 for i in range(1, self.ctx + 1))
        return dx

    def forward(self, x):
        """
        args:
            x: original feature, N x C x T x F or N x T x F
        return:
            y: delta feature, N x C x T x FD or N x T x FD
        """
        delta = [x]
        for _ in range(self.order):
            delta.append(self._add_delta(delta[-1]))
        # N x ... x T x FD
        return th.cat(delta, -1)


class FeatureTransform(nn.Module):
    """
    Feature transform for ASR tasks
        - Spectrogram 
        - MelTransform
        - AbsTransform
        - LogTransform 
        - DiscreteCosineTransform
        - CmvnTransform 
        - SpecAugTransform 
        - SpliceTransform
        - DeltaTransform
    """
    def __init__(self,
                 feats="fbank-log-cmvn",
                 frame_len=400,
                 frame_hop=160,
                 window="hamm",
                 round_pow_of_two=True,
                 sr=16000,
                 num_mels=80,
                 num_ceps=13,
                 lifter=0,
                 aug_prob=0,
                 wrap_step=4,
                 mask_band=30,
                 mask_step=40,
                 num_aug_bands=2,
                 num_aug_steps=2,
                 norm_mean=True,
                 norm_var=True,
                 ds_rate=1,
                 lctx=1,
                 rctx=1,
                 delta_ctx=2,
                 delta_order=2,
                 eps=EPSILON):
        super(FeatureTransform, self).__init__()
        trans_tokens = feats.split("-")
        transform = []
        feats_dim = 0
        downsample_rate = 1
        for tok in trans_tokens:
            if tok == "spectrogram":
                transform.append(
                    SpectrogramTransform(frame_len,
                                         frame_hop,
                                         window=window,
                                         round_pow_of_two=round_pow_of_two))
                feats_dim = transform[-1].dim()
            elif tok == "fbank":
                fbank = [
                    SpectrogramTransform(frame_len,
                                         frame_hop,
                                         window=window,
                                         round_pow_of_two=round_pow_of_two),
                    MelTransform(frame_len,
                                 round_pow_of_two=round_pow_of_two,
                                 sr=sr,
                                 num_mels=num_mels)
                ]
                transform += fbank
                feats_dim = transform[-1].dim()
            elif tok == "mfcc":
                log_fbank = [
                    SpectrogramTransform(frame_len,
                                         frame_hop,
                                         window=window,
                                         round_pow_of_two=round_pow_of_two),
                    MelTransform(frame_len,
                                 round_pow_of_two=round_pow_of_two,
                                 sr=sr,
                                 num_mels=num_mels),
                    LogTransform(eps=eps),
                    DiscreteCosineTransform(num_ceps=num_ceps,
                                            num_mels=num_mels,
                                            lifter=lifter)
                ]
                transform += log_fbank
            elif tok == "mel":
                transform.append(
                    MelTransform(frame_len,
                                 round_pow_of_two=round_pow_of_two,
                                 sr=sr,
                                 num_mels=num_mels))
                feats_dim = transform[-1].dim()
            elif tok == "log":
                transform.append(LogTransform(eps=eps))
            elif tok == "abs":
                transform.append(AbsTransform())
            elif tok == "dct":
                transform.append(
                    DiscreteCosineTransform(num_ceps=num_ceps,
                                            num_mels=num_mels,
                                            lifter=lifter))
                feats_dim = transform[-1].dim()
            elif tok == "cmvn":
                transform.append(
                    CmvnTransform(norm_mean=norm_mean, norm_var=norm_var))
            elif tok == "aug":
                transform.append(
                    SpecAugTransform(p=aug_prob,
                                     wrap_step=wrap_step,
                                     mask_band=mask_band,
                                     mask_step=mask_step,
                                     num_bands=num_aug_bands,
                                     num_steps=num_aug_steps))
            elif tok == "splice":
                transform.append(
                    SpliceTransform(lctx=lctx, rctx=rctx, ds_rate=ds_rate))
                feats_dim *= (1 + lctx + rctx)
                downsample_rate = ds_rate
            elif tok == "delta":
                transform.append(
                    DeltaTransform(ctx=delta_ctx, order=delta_order))
                feats_dim *= (1 + delta_order)
            else:
                raise RuntimeError(f"Unknown token {tok} in {feats}")
        self.transform = nn.Sequential(*transform)
        self.feats_dim = feats_dim
        self.downsample_rate = downsample_rate

    def num_frames(self, x_len):
        """
        Work out number of frames
        """
        return self.transform[0].len(x_len)

    def forward(self, x_pad, x_len):
        """
        args:
            x_pad: raw waveform: N x C x S or N x S
            x_len: N
        return:
            feats_pad: acoustic features: N x C x T x ...
            feats_len: number of frames
        """
        feats_pad = self.transform(x_pad)
        if x_len is None:
            feats_len = None
        else:
            feats_len = self.num_frames(x_len)
            feats_len = feats_len // self.downsample_rate
        return feats_pad, feats_len