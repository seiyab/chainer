import collections
import numpy

import chainer
from chainer.backends import cuda
from chainer import function
from chainer import function_node
from chainer.utils import argument
from chainer.utils import type_check

if cuda.cudnn_enabled:
    cudnn = cuda.cudnn
    libcudnn = cuda.cuda.cudnn


class BatchNormalization(function_node.FunctionNode):

    mean = None
    inv_std = None

    def __init__(self, eps=2e-5, mean=None, var=None, decay=0.9, axis=None):
        self.running_mean = mean
        self.running_var = var

        # Note: cuDNN v5 requires that eps be greater than 1e-5. Otherwise, an
        # error will occur.
        # See CUDNN_BN_MIN_EPSILON value in cudnn.h to verify minimum allowable
        # value.
        self.eps = eps
        if chainer.should_use_cudnn('>=auto'):
            if eps < 1e-5:
                msg = 'cuDNN does not allow an eps value less than 1e-5.'
                raise RuntimeError(msg)
        self.decay = decay
        if isinstance(axis, collections.Sequence):
            pass
        elif isinstance(axis, int):
            axis = axis,
        elif axis is not None:
            raise RuntimeError('axis must be int, tuple of int or None')
        self.axis = axis

    def check_type_forward(self, in_types):
        type_check.expect(in_types.size() == 3)
        x_type, gamma_type, beta_type = in_types
        M = type_check.eval(gamma_type.ndim)
        type_check.expect(
            x_type.dtype.kind == 'f',
            # TODO(beam2d): Check shape
            gamma_type.dtype == x_type.dtype,
            beta_type.dtype == x_type.dtype,
            gamma_type.shape == beta_type.shape,
        )
        if self.axis is None:
            type_check.expect(
                x_type.ndim >= gamma_type.ndim + 1,
                x_type.shape[1:1 + M] == gamma_type.shape,
            )
        else:
            type_check.expect(
                x_type.ndim > len(self.axis),
            )

    def forward(self, inputs):
        self.retain_inputs((0, 1))
        x, gamma, beta = inputs
        xp = cuda.get_array_module(x)
        if self.running_mean is None:
            self.running_mean = xp.zeros_like(gamma)
            self.running_var = xp.zeros_like(gamma)

        if self.axis is None:
            self.axis = (0,) + tuple(range(gamma.ndim + 1, x.ndim))
        self.key_axis = tuple([i for i in range(x.ndim) if i not in self.axis])
        for i, j in enumerate(self.key_axis):
            if gamma.shape[i] != x.shape[j]:
                raise RuntimeError('shape mismatch')

        # expander inserts singleton dimensions to gamma and beta so that they
        # can be broadcasted with x.
        expander = [None for _ in range(x.ndim)]
        for i, j in enumerate(self.key_axis):
            expander[j] = slice(gamma.shape[i])
        self.expander = expander

        self.mode = _BNMode(x, gamma, self.key_axis)
        self.use_cudnn = self.mode.can_use_cudnn(xp)

        if self.use_cudnn:
            x = cuda.cupy.ascontiguousarray(x)

            gamma = cuda.cupy.ascontiguousarray(gamma)
            beta = cuda.cupy.ascontiguousarray(beta)
            dtype = x.dtype
            handle = cudnn.get_handle()
            x_desc = cudnn.create_tensor_descriptor(
                _as4darray(x, self.key_axis))
            cudnn_mode = self.mode.get_cudnn_mode()
            derivedBnDesc = cudnn.create_uninitialized_tensor_descriptor()
            libcudnn.deriveBNTensorDescriptor(derivedBnDesc.value,
                                              x_desc.value, cudnn_mode)
            dtype_param = _get_dtype_of_tensor_descriptor(derivedBnDesc)
            if dtype_param is not dtype:
                gamma = gamma.astype(dtype_param)
                beta = beta.astype(dtype_param)
                running_mean = self.running_mean.astype(dtype_param)
                running_var = self.running_var.astype(dtype_param)
            else:
                running_mean = self.running_mean
                running_var = self.running_var

            oz_dtype = 'd' if x.dtype == 'd' else 'f'
            one = numpy.array(1, dtype=oz_dtype).ctypes
            zero = numpy.array(0, dtype=oz_dtype).ctypes
            y = cuda.cupy.empty_like(x)
            # Factor used in the moving average
            factor = 1 - self.decay

            if self.mean is None:
                # Output cache to speed up backward pass.
                self.mean = xp.empty_like(gamma)
                # Output cache to speed up backward pass.
                self.inv_std = xp.empty_like(gamma)
            # Note: cuDNN computes the mini-batch mean and variance
            # internally. We can simply (optionally) pass
            # it the running-average mean and variance arrays.
            # Note: This API seems to set the inverse of the standard deviation
            # (instead of variance) to resultSaveInvVariance argument. The
            # current implementation of our BN depends on this behavior so that
            # we can reduce the number of reduction kernels.
            libcudnn.batchNormalizationForwardTraining(
                handle, cudnn_mode, one.data, zero.data,
                x_desc.value, x.data.ptr, x_desc.value,
                y.data.ptr, derivedBnDesc.value, gamma.data.ptr,
                beta.data.ptr, factor, running_mean.data.ptr,
                running_var.data.ptr, self.eps,
                self.mean.data.ptr, self.inv_std.data.ptr)

            if dtype_param is not dtype:
                # When data type of prameters is converted, say, from fp16
                # to fp32, the values of fp32 arrays of running_mean and
                # running_var updated by batchNormalizationForwardTraining
                # must be explicitly written back to their original fp16
                # arrays.
                running_mean = running_mean.astype(dtype)
                running_var = running_var.astype(dtype)
                self.running_mean.data.copy_from(running_mean.data,
                                                 running_mean.nbytes)
                self.running_var.data.copy_from(running_var.data,
                                                running_var.nbytes)
        else:
            gamma = gamma[expander]
            beta = beta[expander]
            self.mean = x.mean(axis=self.axis)
            var = x.var(axis=self.axis)
            var += self.eps
            self.inv_std = var ** (-0.5)
            y = _apply_bn_fwd(xp, x, self.mean[expander],
                              self.inv_std[expander], gamma, beta)
            # Update running statistics
            m = x.size // gamma.size
            adjust = m / max(m - 1., 1.)  # unbiased estimation
            self.running_mean *= self.decay
            self.running_mean += (1 - self.decay) * self.mean
            self.running_var *= self.decay
            self.running_var += (1 - self.decay) * adjust * var

        return y,

    def backward(self, indexes, grad_outputs):
        x, gamma = self.get_retained_inputs()
        gy, = grad_outputs
        f = BatchNormalizationGrad(
            self.eps, self.use_cudnn, self.mode, self.expander, self.axis,
            self.mean, self.inv_std, self.key_axis)
        return f(x, gamma, gy)


class BatchNormalizationGrad(function.Function):

    def __init__(self, eps, use_cudnn, mode, expander, axis, mean, inv_std,
                 key_axis):
        self.eps = eps
        self.use_cudnn = use_cudnn
        self.mode = mode
        self.expander = expander
        self.axis = axis
        self.mean = mean
        self.inv_std = inv_std
        self.key_axis = key_axis

    def forward(self, inputs):
        self.retain_inputs((0, 1, 2))
        x, gamma, gy = inputs
        expander = self.expander
        inv_m = gamma.dtype.type(1. / (x.size // gamma.size))
        xp = cuda.get_array_module(x)

        if self.use_cudnn:
            x = cuda.cupy.ascontiguousarray(x)
            gamma = cuda.cupy.ascontiguousarray(gamma)
            gy = cuda.cupy.ascontiguousarray(gy)
            dtype = x.dtype
            handle = cudnn.get_handle()
            x_desc = cudnn.create_tensor_descriptor(
                _as4darray(x, self.key_axis))
            cudnn_mode = self.mode.get_cudnn_mode()
            derivedBnDesc = cudnn.create_uninitialized_tensor_descriptor()
            libcudnn.deriveBNTensorDescriptor(derivedBnDesc.value,
                                              x_desc.value, cudnn_mode)
            dtype_param = _get_dtype_of_tensor_descriptor(derivedBnDesc)
            if dtype_param is not dtype:
                gamma = gamma.astype(dtype_param)
            oz_dtype = 'd' if x.dtype == 'd' else 'f'
            one = numpy.array(1, dtype=oz_dtype).ctypes
            zero = numpy.array(0, dtype=oz_dtype).ctypes
            gx = cuda.cupy.empty_like(x)
            ggamma = cuda.cupy.empty_like(gamma)
            gbeta = cuda.cupy.empty_like(gamma)
            libcudnn.batchNormalizationBackward(
                handle, cudnn_mode, one.data, zero.data,
                one.data, zero.data, x_desc.value, x.data.ptr,
                x_desc.value, gy.data.ptr, x_desc.value, gx.data.ptr,
                derivedBnDesc.value, gamma.data.ptr,
                ggamma.data.ptr, gbeta.data.ptr,
                self.eps, self.mean.data.ptr, self.inv_std.data.ptr)

            if dtype_param is not dtype:
                ggamma = ggamma.astype(dtype)
                gbeta = gbeta.astype(dtype)
        else:
            gbeta = gy.sum(axis=self.axis)
            x_hat = _x_hat(x, self.mean[expander], self.inv_std[expander])
            ggamma = (gy * x_hat).sum(axis=self.axis)
            if xp is numpy:
                gx = (gamma * self.inv_std)[expander] * (
                    gy - (x_hat * ggamma[expander] + gbeta[expander]) * inv_m)
            else:
                gx = cuda.elementwise(
                    '''
                    T gy, T x_hat, T gamma, T inv_std, T ggamma, T gbeta,
                    T inv_m
                    ''',
                    'T gx',
                    '''
                    gx = (gamma * inv_std) * (
                        gy - (x_hat * ggamma + gbeta) * inv_m)
                    ''', 'bn_bwd')(gy, x_hat, gamma[expander],
                                   self.inv_std[expander], ggamma[expander],
                                   gbeta[expander], inv_m)
        self.retain_outputs((0, 1))
        return gx, ggamma, gbeta

    def backward(self, inputs, grad_outputs):
        expander = self.expander

        x, gamma, gy = inputs
        gx1, ggamma1, _ = self.output_data
        ggx1, gggamma1, ggbeta1 = grad_outputs
        xp = cuda.get_array_module(x)

        # auxiliary values
        inv_m = gamma.dtype.type(1. / (x.size // gamma.size))
        r = 0 if ggx1 is None else (gx1 * ggx1).sum(axis=self.axis)
        coeff = gamma * self.inv_std
        coeff_m = coeff * inv_m
        x_hat = _x_hat(x, self.mean[expander], self.inv_std[expander])

        # handle None in output gradients
        ggx1 = _zero_if_none(xp, ggx1, x.shape, x.dtype)
        gggamma1 = _zero_if_none(xp, gggamma1, gamma.shape, gamma.dtype)
        ggbeta1 = _zero_if_none(xp, ggbeta1, gamma.shape, gamma.dtype)

        gggamma2 = gggamma1 - coeff_m * (x_hat * ggx1).sum(axis=self.axis)
        ggbeta2 = ggbeta1 - coeff_m * ggx1.sum(axis=self.axis)

        ggamma2 = r / gamma

        gx_hat2 = (gggamma2[expander] * gy -
                   (coeff_m * ggamma1)[expander] * ggx1)
        gstd2 = -self.inv_std * (r + (x_hat * gx_hat2).sum(axis=self.axis))
        gmean2 = -self.inv_std * gx_hat2.sum(axis=self.axis)
        gx2 = self.inv_std[expander] * gx_hat2 + inv_m * (
            gmean2[expander] + x_hat * gstd2[expander])
        ggy2 = (gggamma2[expander] * x_hat + ggbeta2[expander]
                + coeff[expander] * ggx1)

        return gx2, ggamma2, ggy2


class FixedBatchNormalization(function_node.FunctionNode):

    inv_std = None
    inv_var = None

    def __init__(self, eps=2e-5, axis=None):
        self.eps = eps
        if isinstance(axis, collections.Sequence):
            pass
        elif isinstance(axis, int):
            axis = axis,
        elif axis is not None:
            raise RuntimeError('axis must be int, tuple of int or None')
        self.axis = axis

    def check_type_forward(self, in_types):
        type_check.expect(in_types.size() == 5)
        x_type, gamma_type, beta_type, mean_type, var_type = in_types
        M = type_check.eval(gamma_type.ndim)
        type_check.expect(
            x_type.dtype.kind == 'f',
            # TODO(beam2d): Check shape
            gamma_type.dtype == x_type.dtype,
            beta_type.dtype == x_type.dtype,
            gamma_type.shape == beta_type.shape,
            mean_type.dtype == x_type.dtype,
            mean_type.shape == gamma_type.shape,
            var_type.dtype == x_type.dtype,
            var_type.shape == gamma_type.shape,
        )
        if self.axis is None:
            type_check.expect(
                x_type.ndim >= gamma_type.ndim + 1,
                x_type.shape[1:1 + M] == gamma_type.shape,
            )
        else:
            type_check.expect(
                x_type.ndim > len(self.axis),
            )

    def forward(self, inputs):
        self.retain_inputs((0, 1, 3, 4))
        x, gamma, beta, mean, var = inputs
        xp = cuda.get_array_module(x)

        if self.axis is None:
            self.axis = (0,) + tuple(range(gamma.ndim + 1, x.ndim))
        self.key_axis = tuple([i for i in range(x.ndim) if i not in self.axis])
        for i, j in enumerate(self.key_axis):
            if gamma.shape[i] != x.shape[j]:
                raise RuntimeError('shape mismatch')

        # expander inserts singleton dimensions to gamma and beta so that they
        # can be broadcasted with x.
        expander = [None for _ in range(x.ndim)]
        for i, j in enumerate(self.key_axis):
            expander[j] = slice(gamma.shape[i])
        self.expander = expander

        mode = _BNMode(x, gamma, self.key_axis)
        if mode.can_use_cudnn(xp):
            x = cuda.cupy.ascontiguousarray(x)

            gamma = cuda.cupy.ascontiguousarray(gamma)
            beta = cuda.cupy.ascontiguousarray(beta)
            dtype = x.dtype
            handle = cudnn.get_handle()
            x_desc = cudnn.create_tensor_descriptor(
                _as4darray(x, self.key_axis))
            cudnn_mode = mode.get_cudnn_mode()
            derivedBnDesc = cudnn.create_uninitialized_tensor_descriptor()
            libcudnn.deriveBNTensorDescriptor(derivedBnDesc.value,
                                              x_desc.value, cudnn_mode)
            dtype_param = _get_dtype_of_tensor_descriptor(derivedBnDesc)
            if dtype_param is not dtype:
                gamma = gamma.astype(dtype_param)
                beta = beta.astype(dtype_param)
                mean = mean.astype(dtype_param)
                var = var.astype(dtype_param)
            oz_dtype = 'd' if x.dtype == 'd' else 'f'
            one = numpy.array(1, dtype=oz_dtype).ctypes
            zero = numpy.array(0, dtype=oz_dtype).ctypes
            y = cuda.cupy.empty_like(x)

            libcudnn.batchNormalizationForwardInference(
                handle, cudnn_mode, one.data, zero.data,
                x_desc.value, x.data.ptr, x_desc.value, y.data.ptr,
                derivedBnDesc.value, gamma.data.ptr, beta.data.ptr,
                mean.data.ptr, var.data.ptr, self.eps)
        else:
            gamma = gamma[expander]
            beta = beta[expander]
            var = var + self.eps
            self.inv_var = xp.reciprocal(var)
            self.inv_std = xp.sqrt(self.inv_var, dtype=self.inv_var.dtype)
            y = _apply_bn_fwd(xp, x, mean[expander], self.inv_std[expander],
                              gamma, beta)

        return y,

    def backward(self, indexes, grad_outputs):
        x, gamma, mean, var = self.get_retained_inputs()
        gy, = grad_outputs
        f = FixedBatchNormalizationGrad(
            self.eps, self.expander, self.axis, self.inv_std, self.inv_var)
        return f(x, gamma, mean, var, gy)


class FixedBatchNormalizationGrad(function.Function):

    def __init__(self, eps, expander, axis, inv_std, inv_var):
        self.eps = eps
        self.expander = expander
        self.axis = axis
        self.inv_std = inv_std  # may be None
        self.inv_var = inv_var  # may be None

    def forward(self, inputs):
        self.retain_inputs((0, 1, 2, 4))
        x, gamma, mean, var, gy = inputs
        expander = self.expander
        xp = cuda.get_array_module(x)

        if self.inv_std is None or self.inv_var is None:
            self.inv_var = xp.reciprocal(var + self.eps)
            self.inv_std = xp.sqrt(self.inv_var, dtype=self.inv_var.dtype)

        self.gamma_over_std = gamma * self.inv_std
        x_hat = _x_hat(x, mean[expander], self.inv_std[expander])

        gx = self.gamma_over_std[expander] * gy
        gbeta = gy.sum(axis=self.axis)
        ggamma = (x_hat * gy).sum(axis=self.axis)
        gmean = -self.gamma_over_std * gbeta
        gvar = - 0.5 * gamma * self.inv_var * ggamma

        self.retain_outputs((0, 1, 2, 3, 4))
        return gx, ggamma, gbeta, gmean, gvar

    def backward(self, inputs, grad_outputs):
        x, gamma, mean, _, gy = inputs
        ggx1, gggamma1, ggbeta1, ggmean1, ggvar1 = grad_outputs
        gx1, ggamma1, gbeta1, gmean1, gvar1 = self.output_data

        # Handle None in output gradients.
        xp = cuda.get_array_module(x)
        ggx1 = _zero_if_none(xp, ggx1, x.shape, x.dtype)
        gggamma1 = _zero_if_none(xp, gggamma1, gamma.shape, gamma.dtype)
        ggbeta1 = _zero_if_none(xp, ggbeta1, gamma.shape, gamma.dtype)
        ggmean1 = _zero_if_none(xp, ggmean1, mean.shape, mean.dtype)
        ggvar1 = _zero_if_none(xp, ggvar1, mean.shape, mean.dtype)

        expander = self.expander

        x_hat = _x_hat(x, mean[expander], self.inv_std[expander])
        tmp = -0.5 * ggvar1

        gamma_over_var = gamma * self.inv_var
        g_gamma_over_var = tmp * ggamma1

        gggamma2 = gggamma1 + tmp * gamma_over_var
        gx_hat = gy * gggamma2[expander]
        gx2 = self.inv_std[expander] * gx_hat
        gmean2 = -self.inv_std * gx_hat.sum(axis=self.axis)

        g_gamma_over_std = (ggx1 * gy).sum(axis=self.axis) - ggmean1 * gbeta1
        ggbeta2 = ggbeta1 - ggmean1 * self.gamma_over_std
        ggy2 = (gggamma2[expander] * x_hat + ggbeta2[expander]
                + self.gamma_over_std[expander] * ggx1)

        ggamma2 = (self.inv_var * g_gamma_over_var
                   + self.inv_std * g_gamma_over_std)
        gvar2 = -(ggamma2 * gamma_over_var + 0.5 * self.inv_var * (
            (x_hat * gx_hat).sum(axis=self.axis)
            - self.gamma_over_std * g_gamma_over_std))

        return gx2, ggamma2, gmean2, gvar2, ggy2


class _BNMode(object):

    def __init__(self, x, gamma, key_axis):
        is_gamma_1d = gamma.ndim == 1
        # cuDNN only supports these tensor dimensions because they are
        # the most commonly used. If there is a need to support other
        # dimensions with cuDNN, we could consider reshaping the input
        # into a 2-dim array with channels as second dim and m=<product
        # of all dimensions except the 2nd dimension> as the first
        # dimension.
        self.is_for_conv2d = is_gamma_1d and x.ndim == 4 and key_axis[0] == 1
        self.is_for_linear = is_gamma_1d and key_axis[0] == x.ndim - 1
        self.cudnn_dim_ok = self.is_for_conv2d or self.is_for_linear
        # self.cudnn_dtype_ok = x.dtype != numpy.float16
        self.cudnn_dtype_ok = self.is_for_conv2d or (x.dtype != numpy.float16)

    def get_cudnn_mode(self):
        assert self.cudnn_dim_ok
        # if self.is_for_conv2d:
        #     return libcudnn.CUDNN_BATCHNORM_SPATIAL
        # return libcudnn.CUDNN_BATCHNORM_PER_ACTIVATION
        return libcudnn.CUDNN_BATCHNORM_SPATIAL

    def can_use_cudnn(self, xp):
        # TODO(bkvogel): Check for float16 support again in next cuDNN version.
        # cuDNN v5 batch normalization does not seem to support float16.
        return (xp is not numpy and
                chainer.should_use_cudnn('>=auto', 5000) and
                self.cudnn_dim_ok and
                self.cudnn_dtype_ok)


def _as4darray(arr, key_axis):
    if arr.ndim == 4 and key_axis[0] == 1:
        return arr
    elif key_axis[0] == arr.ndim - 1:
        return arr.reshape(numpy.prod(arr.shape[0:-1]), -1, 1, 1)
    else:
        msg = 'Unexpected combination of array shape and key_axis'
        raise RuntimeError(msg)


def _get_mode(x, gamma):
    if x.ndim == 4 and gamma.ndim == 1:
        return libcudnn.CUDNN_BATCHNORM_SPATIAL
    return libcudnn.CUDNN_BATCHNORM_PER_ACTIVATION


def _x_hat(x, mean, inv_std):
    x_mu = x - mean
    x_mu *= inv_std
    return x_mu


def _apply_bn_fwd(xp, x, mean, inv_std, gamma, beta):
    # NOTE: all arguments should be broadcasted to x.shape
    # (mean, inv_std, gamma, and beta have to already be expanded)
    if xp is numpy:
        x_hat = _x_hat(x, mean, inv_std)
        y = gamma * x_hat
        y += beta
    else:
        y = cuda.elementwise(
            'T x, T mean, T inv_std, T gamma, T beta', 'T y',
            'y = gamma * (x - mean) * inv_std + beta', 'bn_fwd'
        )(x, mean, inv_std, gamma, beta)
    return y


def _zero_if_none(xp, x, shape, dtype):
    # TODO(Tokui): Return broadcasted 0 instead of a zeroed array.
    if x is None:
        return xp.zeros(shape, dtype=dtype)
    return x


def _get_dtype_of_tensor_descriptor(desc):
    cudnn_dtype, _, _, _, _, _, _, _, _ = libcudnn.getTensor4dDescriptor(
        desc.value)
    dtype = None
    if cudnn_dtype == libcudnn.CUDNN_DATA_DOUBLE:
        dtype = numpy.dtype(numpy.float64)
    elif cudnn_dtype == libcudnn.CUDNN_DATA_FLOAT:
        dtype = numpy.dtype(numpy.float32)
    elif cudnn_dtype == libcudnn.CUDNN_DATA_HALF:
        dtype = numpy.dtype(numpy.float16)
    else:
        msg = 'Unknow cudnn data type {} '.format(cudnn_dtype)
        raise RuntimeError(msg)
    return dtype


def batch_normalization(x, gamma, beta, **kwargs):
    """batch_normalization(x, gamma, beta, eps=2e-5, running_mean=None, running_var=None, decay=0.9)

    Batch normalization function.

    It takes the input variable ``x`` and two parameter variables ``gamma`` and
    ``beta``. The parameter variables must both have the same dimensionality,
    which is referred to as the channel shape. This channel shape corresponds
    to the dimensions in the input which are not averaged over. Since the
    first dimension of the input corresponds to the batch size, the second
    dimension of `x` will correspond to the first dimension of the channel
    shape, the third dimension of `x` will correspond to the second channel
    dimension (if it exists) and so on. Therefore, the dimensionality of the
    input must be at least one plus the number of channel dimensions. The
    total effective "batch size" will then be considered to be the product of
    all dimensions in `x` except for the channel dimensions.

    As an example, if the input is four dimensional and the parameter
    variables are one dimensional, then it is assumed that the first
    dimension of the input is the batch size, the second dimension is the
    channel size, and the remaining two dimensions are considered
    to be spatial dimensions that will be averaged over along with the
    batch size in the batch normalization computations. That is,
    the total batch size will be considered to be the product of all
    input dimensions except the second dimension.

    Note: If this function is called, it will not be possible to access the
    updated running mean and variance statistics, because they are members
    of the function object, which cannot be accessed by the caller.
    If it is desired to access the updated running statistics, it is necessary
    to get a new instance of the function object, call the object, and then
    access the running_mean and/or running_var attributes. See the
    corresponding Link class for an example of how to do this.

    .. warning::

       ``train`` argument is not supported anymore since v2.
       Instead, use ``chainer.using_config('train', train)``.
       See :func:`chainer.using_config`.

    Args:
        x (Variable): Input variable.
        gamma (Variable): Scaling parameter of normalized data.
        beta (Variable): Shifting parameter of scaled normalized data.
        eps (float): Epsilon value for numerical stability.
        running_mean (numpy.ndarray or cupy.ndarray):
            Running average of the mean. This is a
            running average of the mean over several mini-batches using
            the decay parameter. If ``None``, the running average is not
            computed. If this is ``None``, then ``runnng_var`` must also
            be ``None``.
        running_var (numpy.ndarray or cupy.ndarray):
            Running average of the variance. This is a
            running average of the variance over several mini-batches using
            the decay parameter. If ``None``, the running average is not
            computed. If this is ``None``, then ``running_mean`` must also
            be ``None``.
        decay (float): Decay rate of moving average. It is used during
            training.

    See: `Batch Normalization: Accelerating Deep Network Training by Reducing\
          Internal Covariate Shift <https://arxiv.org/abs/1502.03167>`_

    .. seealso:: :class:`links.BatchNormalization`

    """  # NOQA

    argument.check_unexpected_kwargs(
        kwargs, train='train argument is not supported anymore. '
        'Use chainer.using_config')
    eps, running_mean, running_var, decay, axis = argument.parse_kwargs(
        kwargs, ('eps', 2e-5), ('running_mean', None),
        ('running_var', None), ('decay', 0.9), ('axis', None))

    return BatchNormalization(eps, running_mean, running_var, decay,
                              axis).apply((x, gamma, beta))[0]


def fixed_batch_normalization(x, gamma, beta, mean, var, eps=2e-5, axis=None):
    """Batch normalization function with fixed statistics.

    This is a variant of batch normalization, where the mean and variance
    statistics are given by the caller as fixed variables. This is
    used on testing mode of the batch normalization layer, where batch
    statistics cannot be used for prediction consistency.

    Args:
        x (Variable): Input variable.
        gamma (Variable): Scaling parameter of normalized data.
        beta (Variable): Shifting parameter of scaled normalized data.
        mean (Variable): Shifting parameter of input.
        var (Variable): Square of scaling parameter of input.
        eps (float): Epsilon value for numerical stability.

    .. seealso::
       :func:`functions.batch_normalization`,
       :class:`links.BatchNormalization`

    """
    return FixedBatchNormalization(eps, axis).apply((x, gamma, beta, mean,
                                                     var))[0]
