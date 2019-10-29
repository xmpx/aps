#!/usr/bin/env python

# wujian@2019

import random

import torch as th
import torch.nn as nn

import torch.nn.functional as F

from torch.nn.utils.rnn import pack_padded_sequence

from nn.decoder import TorchDecoder
from nn.encoder import encoder_instance
from nn.attention import att_instance


class Seq2Seq(nn.Module):
    """
    A simple attention based sequence-to-sequence model
    """
    def __init__(
            self,
            input_size=80,
            vocab_size=30,
            sos=-1,
            eos=-1,
            transform=None,
            att_type="ctx",
            att_kwargs=None,
            # encoder
            encoder_type="common",
            encoder_proj=256,
            encoder_kwargs=None,
            # decoder
            decoder_dim=512,
            decoder_kwargs=None):
        super(Seq2Seq, self).__init__()
        self.encoder = encoder_instance(encoder_type, input_size, encoder_proj,
                                        **encoder_kwargs)
        attend = att_instance(att_type, encoder_proj, decoder_dim,
                              **att_kwargs)
        self.decoder = TorchDecoder(encoder_proj + decoder_dim,
                                    vocab_size,
                                    attention=attend,
                                    **decoder_kwargs)
        if not eos or not sos:
            raise RuntimeError("Unsupported SOS/EOS "
                               "value: {:d}/{:d}".format(sos, eos))
        self.sos = sos
        self.eos = eos
        self.transform = transform

    def forward(self, x_pad, x_len, y_pad, ssr=0):
        """
        args:
            x_pad: N x Ti x D or N x S
            x_len: N or None
            y_pad: N x To
            ssr: schedule sampling rate
        return:
            outs: N x (To+1) x V
            alis: N x (To+1) x T
        """
        # feature transform
        if self.transform:
            x_pad, x_len = self.transform(x_pad, x_len)
        # N x Ti x D
        enc_out, enc_len = self.encoder(x_pad, x_len)
        # N x (To+1), pad SOS
        outs, alis = self.decoder(enc_out,
                                  enc_len,
                                  y_pad,
                                  sos=self.sos,
                                  schedule_sampling=ssr)
        return outs, alis

    def beam_search(self, x, beam=8, nbest=5, max_len=None):
        """
        args
            x: Ti x F
        """
        if x.dim() != 2:
            raise RuntimeError("Now only support for one utterance")

        with th.no_grad():
            if self.transform:
                x, _ = self.transform(x.unsqueeze(0), None)
                enc_out, _ = self.encoder(x, None)
            else:
                # 1 x Ti x F
                enc_out, _ = self.encoder(x.unsqueeze(0), None)
            return self.decoder.beam_search(enc_out,
                                            beam=beam,
                                            nbest=nbest,
                                            max_len=max_len,
                                            sos=self.sos,
                                            eos=self.eos)