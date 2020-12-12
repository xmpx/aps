#!/usr/bin/env python

# Copyright 2019 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

import torch as th
import torch.nn as nn

from typing import Optional, Dict, Tuple, List
from torch_complex import ComplexTensor

from aps.asr.att import AttASR, AttASROutputType
from aps.asr.base.encoder import PyTorchRNNEncoder
from aps.asr.filter.mvdr import MvdrBeamformer
from aps.asr.filter.google import CLPFsBeamformer  # same as TimeInvariantFilter
from aps.asr.filter.conv import (TimeInvariantFilter, TimeVariantFilter,
                                 TimeInvariantAttFilter)
from aps.libs import ApsRegisters


class EnhAttASR(nn.Module):
    """
    AttASR with enhancement front-end
    """

    def __init__(
            self,
            asr_input_size: int = 80,
            vocab_size: int = 30,
            sos: int = -1,
            eos: int = -1,
            # feature transform
            asr_transform: Optional[nn.Module] = None,
            asr_cpt: str = "",
            ctc: bool = False,
            # attention
            att_type: str = "ctx",
            att_kwargs: Dict = {},
            # encoder
            enc_type: str = "common",
            enc_proj: int = 256,
            enc_kwargs: Dict = {},
            # decoder
            dec_dim: int = 512,
            dec_kwargs: Dict = {}) -> None:
        super(EnhAttASR, self).__init__()
        # Back-end feature transform
        self.asr_transform = asr_transform
        # LAS-based ASR
        self.las_asr = AttASR(input_size=asr_input_size,
                              vocab_size=vocab_size,
                              eos=eos,
                              sos=sos,
                              ctc=ctc,
                              asr_transform=None,
                              att_type=att_type,
                              att_kwargs=att_kwargs,
                              enc_type=enc_type,
                              enc_proj=enc_proj,
                              enc_kwargs=enc_kwargs,
                              dec_dim=dec_dim,
                              dec_kwargs=dec_kwargs)
        if asr_cpt:
            las_cpt = th.load(asr_cpt, map_location="cpu")
            self.las_asr.load_state_dict(las_cpt, strict=False)
        self.sos = sos
        self.eos = eos

    def _enhance(self, x_pad, x_len):
        """
        Enhancement and asr feature transform
        """
        raise NotImplementedError

    def forward(self,
                x_pad: th.Tensor,
                x_len: Optional[th.Tensor],
                y_pad: th.Tensor,
                ssr: float = 0) -> AttASROutputType:
        """
        Args:
            x_pad: N x Ti x D or N x S
            x_len: N or None
            y_pad: N x To
            ssr: schedule sampling rate
        Return:
            outs: N x (To+1) x V
            alis: N x (To+1) x T
            ...
        """
        # mvdr beamforming: N x Ti x F
        x_enh, x_len = self._enhance(x_pad, x_len)
        # outs, alis, ctc_branch, ...
        return self.las_asr(x_enh, x_len, y_pad, ssr=ssr)

    def beam_search(self,
                    x: th.Tensor,
                    lm: Optional[nn.Module] = None,
                    lm_weight: float = 0,
                    beam: int = 16,
                    nbest: int = 8,
                    max_len: int = -1,
                    penalty: float = 0,
                    normalized: bool = True,
                    temperature: float = 1) -> List[Dict]:
        """
        Args
            x (Tensor): C x S
        """
        with th.no_grad():
            if x.dim() != 2:
                raise RuntimeError("Now only support for one utterance")
            x_enh, _ = self._enhance(x[None, ...], None)
            return self.las_asr.beam_search(x_enh[0],
                                            lm=lm,
                                            lm_weight=lm_weight,
                                            beam=beam,
                                            nbest=nbest,
                                            max_len=max_len,
                                            penalty=penalty,
                                            normalized=normalized,
                                            temperature=temperature)

    def beam_search_batch(self,
                          x: th.Tensor,
                          x_len: Optional[th.Tensor],
                          lm: Optional[nn.Module] = None,
                          lm_weight: float = 0,
                          beam: int = 16,
                          nbest: int = 8,
                          max_len: int = -1,
                          penalty: float = 0,
                          normalized: bool = True,
                          temperature: float = 1) -> List[Dict]:
        """
        Args
            x (Tensor): N x C x S
        """
        with th.no_grad():
            x_enh, x_len = self._enhance(x, x_len)
            return self.las_asr.beam_search_batch(x_enh,
                                                  x_len,
                                                  lm=lm,
                                                  lm_weight=lm_weight,
                                                  beam=beam,
                                                  nbest=nbest,
                                                  max_len=max_len,
                                                  penalty=penalty,
                                                  normalized=normalized,
                                                  temperature=temperature)


@ApsRegisters.asr.register("mvdr_att")
class MvdrAttASR(EnhAttASR):
    """
    Mvdr beamformer + Att-based ASR model
    """

    def __init__(
            self,
            enh_input_size: int = 257,
            num_bins: int = 257,
            # beamforming
            enh_transform: Optional[nn.Module] = None,
            mask_net_kwargs: Optional[Dict] = None,
            mask_net_noise: bool = False,
            mvdr_kwargs: Optional[Dict] = None,
            **kwargs) -> None:
        super(MvdrAttASR, self).__init__(**kwargs)
        if enh_transform is None:
            raise RuntimeError("Enhancement feature transform can not be None")
        # Front-end feature extraction
        self.enh_transform = enh_transform
        # TF-mask estimation network
        self.mask_net = PyTorchRNNEncoder(
            enh_input_size, num_bins * 2 if mask_net_noise else num_bins,
            **mask_net_kwargs)
        self.mask_net_noise = mask_net_noise
        # MVDR beamformer
        self.mvdr_net = MvdrBeamformer(num_bins, **mvdr_kwargs)

    def _enhance(
            self, x_pad: th.Tensor, x_len: Optional[th.Tensor]
    ) -> Tuple[th.Tensor, Optional[th.Tensor]]:
        """
        Mvdr beamforming and asr feature transform
        """
        # mvdr beamforming: N x Ti x F
        x_beam, x_len = self.mvdr_beam(x_pad, x_len)
        # asr feature transform
        x_beam, _ = self.asr_transform(x_beam, None)
        return x_beam, x_len

    def mvdr_beam(
            self, x_pad: th.Tensor, x_len: Optional[th.Tensor]
    ) -> Tuple[th.Tensor, Optional[th.Tensor]]:
        """
        Mvdr beamforming and asr feature transform
        Args:
            x_pad: Tensor, N x C x S
            x_len: Tensor, N or None
        """
        # TF-mask
        mask_s, mask_n, x_len, x_cplx = self.pred_mask(x_pad, x_len)
        # mvdr beamforming: N x Ti x F
        x_beam = self.mvdr_net(mask_s, x_cplx, xlen=x_len, mask_n=mask_n)
        return x_beam, x_len

    def pred_mask(
        self, x_pad: th.Tensor, x_len: Optional[th.Tensor]
    ) -> Tuple[th.Tensor, Optional[th.Tensor], Optional[th.Tensor],
               ComplexTensor]:
        """
        Output TF masks
        Args:
            x_pad: Tensor, N x C x S
            x_len: Tensor, N or None
        """
        # enhancement feature transform
        x_pad, x_cplx, x_len = self.enh_transform(x_pad, x_len)
        # TF-mask estimation: N x T x F
        x_mask, x_len = self.mask_net(x_pad, x_len)
        if self.mask_net_noise:
            mask_s, mask_n = th.chunk(x_mask, 2, dim=-1)
        else:
            mask_s, mask_n = x_mask, None
        return mask_s, mask_n, x_len, x_cplx


@ApsRegisters.asr.register("beam_att")
class BeamAttASR(EnhAttASR):
    """
    Beamformer-based front-end + AttASR
    """

    def __init__(self,
                 mode: str = "tv",
                 enh_transform: Optional[nn.Module] = None,
                 enh_kwargs: Optional[Dict] = None,
                 **kwargs) -> None:
        super(BeamAttASR, self).__init__(**kwargs)
        conv_enh = {
            "ti": TimeInvariantFilter,
            "tv": TimeVariantFilter,
            "ti_att": TimeInvariantAttFilter,
            "clp": CLPFsBeamformer
        }
        if mode not in conv_enh:
            raise RuntimeError(f"Unknown fs mode: {mode}")
        if enh_transform is None:
            raise RuntimeError("enh_transform can not be None")
        self.enh = conv_enh[mode](**enh_kwargs)
        self.enh_transform = enh_transform

    def _enhance(
            self, x_pad: th.Tensor, x_len: Optional[th.Tensor]
    ) -> Tuple[th.Tensor, Optional[th.Tensor]]:
        """
        FE processing
        """
        _, x_pad, x_len = self.enh_transform(x_pad, x_len)
        # N x T x D
        x_enh = self.enh(x_pad)
        return x_enh, x_len
