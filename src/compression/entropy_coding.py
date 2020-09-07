"""
Based on many sources, mainly:

Fabian Gielsen's ANS implementation: https://github.com/rygorous/ryg_rans
Craystack ANS implementation: https://github.com/j-towns/craystack/blob/master/craystack/rans.py
"""

OVERFLOW_WIDTH = 4
OVERFLOW_CODE = 1 << (1 << OVERFLOW_WIDTH)
PATCH_SIZE = (4,4)

import torch
import numpy as np

from warnings import warn
from collections import namedtuple

# Custom
from src.helpers import maths
from src.compression import ans as vrans
from src.compression import compression_utils

Codec = namedtuple('Codec', ['push', 'pop'])
cast2u64 = lambda x: np.array(x, dtype=np.uint64)

def base_codec(enc_statfun, dec_statfun, precision):
    if np.any(precision >= 24):
        warn('Detected precision over 28. Codecs lose accuracy at high '
             'precision.')

    def push(message, symbol):
        start, freq = enc_statfun(symbol)
        return vrans.push(message, start, freq, precision)

    def pop(message):
        cf, pop_fun = vrans.pop(message, precision)
        symbol = dec_statfun(cf)
        start, freq = enc_statfun(symbol)
        assert np.all(start <= cf) and np.all(cf < start + freq)
        return pop_fun(start, freq), symbol

    return Codec(push, pop)

def _indexed_cdf_to_enc_statfun(cdf_i):
    # enc_statfun: symbol |-> start, freq
    def _enc_statfun(value):
        # Value in [0, max_length]
        lower = cdf_i[value]
        # cum_freq, pmf @ value
        return lower, cdf_i[int(value + np.uint64(1))] - lower
            
    return _enc_statfun

def _vec_indexed_cdf_to_enc_statfun(cdf_i):
    # enc_statfun: symbol |-> start, freq
    def _enc_statfun(value):
        # (coding_shape) = (C,H,W) by default but can  be generalized
        # cdf_i: [(coding_shape), pmf_length + 2]
        # value: [(coding_shape)]
        lower = np.squeeze(np.take_along_axis(cdf_i, 
            np.expand_dims(value, -1), axis=-1))
        upper = np.squeeze(np.take_along_axis(cdf_i, 
            np.expand_dims(value + 1, -1), axis=-1))

        return lower, upper - lower

    return _enc_statfun

def _indexed_cdf_to_dec_statfun(cdf_i, cdf_i_length):
    # dec_statfun: cf |-> symbol
    cdf_i = cdf_i[:cdf_i_length]
    term = cdf_i[-1]
    assert term == OVERFLOW_CODE or term == 1 << OVERFLOW_WIDTH, (
        f"{cdf_i[-1]} expected to be overflow value."
    )

    def _dec_statfun(cum_freq):
        # cum_freq in [0, 2 ** precision]
        # Search such that CDF[s] <= cum_freq < CDF[s+1]
        sym = np.searchsorted(cdf_i, cum_freq, side='right') - 1
        return sym

    return _dec_statfun

def _vec_indexed_cdf_to_dec_statfun(cdf_i, cdf_i_length):
    # dec_statfun: cf |-> symbol
    *coding_shape, max_cdf_length = cdf_i.shape
    coding_shape = tuple(coding_shape)
    cdf_i_flat = np.reshape(cdf_i, (-1, max_cdf_length))

    cdf_i_flat_ragged = [c[:l] for (c,l) in zip(cdf_i_flat, 
        cdf_i_length.flatten())]

    def _dec_statfun(value):
        # (coding_shape) = (C,H,W) by default but can be generalized
        # cdf_i: [(coding_shape), pmf_length + 2]
        # value: [(coding_shape)]
        assert value.shape == coding_shape, "CDF-value shape mismatch!"
        sym_flat = np.array(
            [np.searchsorted(cb, v_i, 'right') - 1 for (cb, v_i) in 
                zip(cdf_i_flat_ragged, value.flatten())])

        sym = np.reshape(sym_flat, coding_shape)
        return sym  # (coding_shape)

    return _dec_statfun

def ans_index_encoder(symbols, indices, cdf, cdf_length, cdf_offset, precision, 
    overflow_width=OVERFLOW_WIDTH, **kwargs):
    """
    ANS-encodes unbounded integer data using an indexed probability table. 
    Encodes scalars sequentially.

    For each value in data, the corresponding value in index determines which probability model 
    in cdf is used to encode it. The data can be arbitrary signed integers, where the integer 
    intervals determined by offset and cdf_size are modeled using the cumulative distribution 
    functions (CDF) in `cdf`. Everything else is encoded with a variable length code.

    The argument `cdf` is a 2-D tensor and its each row contains a CDF. The argument
    `cdf_size` is a 1-D tensor, and its length should be the same as the number of
    rows of `cdf`. The values in `cdf_size` denotes the length of CDF vector in the
    corresponding row of `cdf`.

    For i = 0,1,..., let `m = cdf_size[i]`. Then for j = 0,1,...,m-1,

    ```
    cdf[..., 0] / 2^precision = Pr(X < 0) = 0
    cdf[..., 1] / 2^precision = Pr(X < 1) = Pr(X <= 0)
    cdf[..., 2] / 2^precision = Pr(X < 2) = Pr(X <= 1)
    ...
    cdf[..., m-1] / 2^precision = Pr(X < m-1) = Pr(X <= m-2).
    ```

    We require that `1 < m <= cdf.shape[1]` and that all elements of `cdf` be in the
    closed interval `[0, 2^precision]`.

    Arguments `data` and `index` should have the same shape. `data` contains the
    values to be encoded. `index` denotes which row in `cdf` should be used to
    encode the corresponding value in `data`, and which element in `offset`
    determines the integer interval the cdf applies to. Naturally, the elements of
    `index` should be in the half-open interval `[0, cdf.shape[0])`.

    When a value from `data` is in the interval `[offset[i], offset[i] + m - 2)`,
    then the value is range encoded using the CDF values. The last entry in each
    CDF (the one at `m - 1`) is an overflow code. When a value from `data` is
    outside of the given interval, the overflow value is encoded, followed by a
    variable-length encoding of the actual data value.

    The encoded output contains neither the shape information of the encoded data
    nor a termination symbol. Therefore the shape of the encoded data must be
    explicitly provided to the decoder.

    symbols <-> indices
    cdf <-> cdf_offset <-> cdf_length
    """

    message = vrans.empty_message(())
    coding_shape = symbols.shape[1:]
    symbols = symbols.astype(np.int32).flatten()
    indices = indices.astype(np.int32).flatten()

    max_overflow = (1 << overflow_width) - 1
    overflow_cdf_size = (1 << overflow_width) + 1
    overflow_cdf = np.arange(overflow_cdf_size, dtype=np.uint64)

    enc_statfun_overflow = _indexed_cdf_to_enc_statfun(overflow_cdf)
    dec_statfun_overflow = _indexed_cdf_to_dec_statfun(overflow_cdf,
        len(overflow_cdf))
    overflow_push, overflow_pop = base_codec(enc_statfun_overflow,
        dec_statfun_overflow, overflow_width)

    # LIFO - last item compressed is first item decompressed
    for i in reversed(range(len(indices))):  # loop over flattened axis

        cdf_index = indices[i]
        cdf_i = cdf[cdf_index]
        cdf_length_i = cdf_length[cdf_index]

        assert (cdf_index >= 0 and cdf_index < cdf.shape[0]), (
            f"Invalid index {cdf_index} for symbol {i}")

        max_value = cdf_length_i - 2

        assert max_value >= 0 and max_value < cdf.shape[1] - 1, (
            f"Invalid max length {max_value} for symbol {i}")

        # Data in range [offset[cdf_index], offset[cdf_index] + m - 2] is ANS-encoded
        # Map values with tracked probabilities to range [0, ..., max_value]
        value = symbols[i]
        value -= cdf_offset[cdf_index]

        # If outside of this range, map value to non-negative integer overflow.
        overflow = 0
        if (value < 0):
            overflow = -2 * value - 1
            value = max_value
        elif (value >= max_value):
            overflow = 2 * (value - max_value)
            value = max_value

        assert value >= 0 and value < cdf_length_i - 1, (
            f"Invalid shifted value {value} for symbol {i} w/ "
            f"cdf_length {cdf_length[cdf_index]}")

        # Bin of discrete CDF that value belongs to
        enc_statfun = _indexed_cdf_to_enc_statfun(cdf_i)
        dec_statfun = _indexed_cdf_to_dec_statfun(cdf_i, cdf_length_i)
        symbol_push, symbol_pop = base_codec(enc_statfun, dec_statfun, precision)

        message = symbol_push(message, value)

        # When value is outside of the given interval, the overflow value is encoded,
        # followed by a variable-length encoding of the actual data value.
        if value == max_value:
            pass
            # widths = 0
            # while ((overflow >> (widths * overflow_width)) != 0):
            #     widths += 1

            # val = widths
            # while (val >= max_overflow):
            #     message = overflow_push(message, cast2u64(max_overflow))
            #     val -= max_overflow
            
            # message = overflow_push(message, cast2u64(val))

            # for j in range(widths):
            #     val = (overflow >> (j * overflow_width)) & max_overflow
            #     message = overflow_push(message, cast2u64(val))


    encoded = vrans.flatten(message)
    message_length = len(encoded)
    # print('Symbol compressed to {:.3f} bits.'.format(32 * message_length))
    return encoded, coding_shape


def vec_ans_index_encoder(symbols, indices, cdf, cdf_length, cdf_offset, precision, 
    coding_shape, overflow_width=OVERFLOW_WIDTH, **kwargs):
    """
    Vectorized version of `ans_index_encoder`. Incurs constant bit overhead, 
    but is faster.

    ANS-encodes unbounded integer data using an indexed probability table.
    """
    
    symbols_shape = symbols.shape
    B, n_channels = symbols_shape[:2]
    symbols = symbols.astype(np.int32)
    indices = indices.astype(np.int32)
    cdf_index = indices
        
    assert bool(np.all(cdf_index >= 0)) and bool(np.all(cdf_index < cdf.shape[0])), (
        "Invalid index.")

    max_value = cdf_length[cdf_index] - 2

    assert bool(np.all(max_value >= 0)) and bool(np.all(max_value < cdf.shape[1] - 1)), (
        "Invalid max length.")

    # Map values with tracked probabilities to range [0, ..., max_value]
    values = symbols - cdf_offset[cdf_index]

    # If outside of this range, map value to non-negative integer overflow.
    overflow = np.zeros_like(values)

    of_mask = values < 0
    overflow = np.where(of_mask, -2 * values - 1, overflow)
    values = np.where(of_mask, max_value, values)

    of_mask = values >= max_value
    overflow = np.where(of_mask, 2 * (values - max_value), overflow)
    values = np.where(of_mask, max_value, values)

    assert bool(np.all(values >= 0)), (
        "Invalid shifted value for current symbol - values must be non-negative.")

    assert bool(np.all(values < cdf_length[cdf_index] - 1)), (
        "Invalid shifted value for current symbol - outside cdf index bounds.")

    if B == 1:
        # Vectorize on patches
        print(symbols_shape[2])
        print(symbols_shape[3])
        assert (symbols_shape[2] % PATCH_SIZE[0] == 0) and (symbols_shape[3] % PATCH_SIZE[1] == 0)
        values, _ = compression_utils.decompose(values, n_channels)
        cdf_index, unfolded_shape = compression_utils.decompose(indices, n_channels)
        coding_shape = values.shape[1:]

    message = vrans.empty_message(coding_shape)

    # LIFO - last item compressed is first item decompressed
    for i in reversed(range(len(cdf_index))):  # loop over batch dimension
        # Bin of discrete CDF that value belongs to
        value_i = values[i]
        cdf_index_i = cdf_index[i]        
        cdf_i = cdf[cdf_index_i]
        cdf_i_length = cdf_length[cdf_index_i]

        enc_statfun = _vec_indexed_cdf_to_enc_statfun(cdf_i)
        dec_statfun = _vec_indexed_cdf_to_dec_statfun(cdf_i, cdf_i_length)
        symbol_push, symbol_pop = base_codec(enc_statfun, dec_statfun, precision)

        message = symbol_push(message, value_i)

        """
        Also encode overflows here
        """

    encoded = vrans.flatten(message)
    message_length = len(encoded)

    # print('{} symbols compressed to {:.3f} bits.'.format(B, 32 * message_length))

    return encoded, coding_shape


def ans_index_decoder(encoded, indices, cdf, cdf_length, cdf_offset, precision,
    coding_shape, overflow_width=OVERFLOW_WIDTH, **kwargs):

    """
    Reverse op of `ans_index_encoder`. Decodes ans-encoded bitstring `encoded` into 
    a decoded message tensor `decoded.

    Arguments (`indices`, `cdf`, `cdf_length`, `cdf_offset`, `precision`) must be 
    identical to the inputs to the encoding function used to generate the encoded 
    tensor.
    """

    message = vrans.unflatten_scalar(encoded)  # (head, tail)
    decoded = np.empty(indices.shape).flatten()
    indices = indices.astype(np.int32).flatten()

    max_overflow = (1 << overflow_width) - 1
    overflow_cdf_size = (1 << overflow_width) + 1
    overflow_cdf = np.arange(overflow_cdf_size, dtype=np.uint64)

    enc_statfun_overflow = _indexed_cdf_to_enc_statfun(overflow_cdf)
    dec_statfun_overflow = _indexed_cdf_to_dec_statfun(overflow_cdf,
        len(overflow_cdf))
    overflow_push, overflow_pop = base_codec(enc_statfun_overflow,
        dec_statfun_overflow, overflow_width)

    for i in range(len(indices)):

        cdf_index = indices[i]
        cdf_i = cdf[cdf_index]
        cdf_length_i = cdf_length[cdf_index]

        assert (cdf_index >= 0 and cdf_index < cdf.shape[0]), (
            f"Invalid index {cdf_index} for symbol {i}")

        max_value = cdf_length_i - 2

        assert max_value >= 0 and max_value < cdf.shape[1] - 1, (
            f"Invalid max length {max_value} for symbol {i}")

        # Bin of discrete CDF that value belongs to
        enc_statfun = _indexed_cdf_to_enc_statfun(cdf_i)
        dec_statfun = _indexed_cdf_to_dec_statfun(cdf_i, cdf_length_i)
        symbol_push, symbol_pop = base_codec(enc_statfun, dec_statfun, precision)

        message, value = symbol_pop(message)

        """
        Handle overflow values
        """

        if value == max_value:
            pass
            # message, val = overflow_pop(message)
            # val = int(val)
            # widths = val

            # while val == max_overflow:
            #     message, val = overflow_pop(message)
            #     val = int(val)
            #     widths += val
            
            # overflow = 0
            # print(widths)
            # for j in range(widths):
            #     message, val = overflow_pop(message)
            #     val = int(val)
            #     assert val <= max_overflow
            #     overflow |= val << (j * overflow_width)

            # # Map positive values back to integer values.
            # value = overflow >> 1
            # if (overflow & 1):
            #     value = -value - 1
            # else:
            #     value += max_value
        
        symbol = value + cdf_offset[cdf_index]
        decoded[i] = symbol

    return decoded


def vec_ans_index_decoder(encoded, indices, cdf, cdf_length, cdf_offset, precision, 
    coding_shape, overflow_width=OVERFLOW_WIDTH, **kwargs):

    """
    Reverse op of `vec_ans_index_encoder`. Decodes ans-encoded bitstring into a decoded 
    message tensor.
    Arguments (`indices`, `cdf`, `cdf_length`, `cdf_offset`, `precision`) must be 
    identical to the inputs to `vec_ans_index_encoder` used to generate the encoded tensor.
    """

    original_shape = indices.shape
    B, n_channels, *_ = original_shape
    message = vrans.unflatten(encoded, coding_shape)
    indices = indices.astype(np.int32)
    cdf_index = indices
        
    assert bool(np.all(cdf_index >= 0)) and bool(np.all(cdf_index < cdf.shape[0])), (
        "Invalid index.")

    max_value = cdf_length[cdf_index] - 2

    assert bool(np.all(max_value >= 0)) and bool(np.all(max_value < cdf.shape[1] - 1)), (
        "Invalid max length.")

    if B == 1:
        # Vectorize on patches - there's probably a way to interlace patches with
        # batch elements for B > 1 ...
        indices, unfolded_shape = compression_utils.decompose(indices, n_channels)
        cdf_index = indices
        assert indices.shape[1:] == coding_shape, 'Shape of vectorized patches invalid.'

    symbols = []
    for i in range(len(cdf_index)):
        cdf_index_i = cdf_index[i]
        cdf_i = cdf[cdf_index_i]
        cdf_length_i = cdf_length[cdf_index_i]

        enc_statfun = _vec_indexed_cdf_to_enc_statfun(cdf_i)
        dec_statfun = _vec_indexed_cdf_to_dec_statfun(cdf_i, cdf_length_i)
        symbol_push, symbol_pop = base_codec(enc_statfun, dec_statfun, precision)

        message, value = symbol_pop(message)
        symbol = value + cdf_offset[cdf_index_i]
        symbols.append(symbol)

    if B == 1:
        decoded = compression_utils.reconstitute(np.stack(symbols, axis=0), original_shape, unfolded_shape)
    else:
        decoded = np.stack(symbols, axis=0)
    return decoded

def ans_encode_decode_test(symbols, decompressed_symbols):
    return np.testing.assert_almost_equal(symbols, decompressed_symbols)

