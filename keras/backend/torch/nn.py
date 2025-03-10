import torch
import torch.nn.functional as tnn
import tree

from keras.backend import standardize_data_format
from keras.backend import standardize_dtype
from keras.backend.common.backend_utils import (
    compute_conv_transpose_padding_args_for_torch,
)
from keras.backend.config import epsilon
from keras.backend.torch.core import cast
from keras.backend.torch.core import convert_to_tensor
from keras.backend.torch.core import get_device
from keras.backend.torch.numpy import expand_dims
from keras.backend.torch.numpy import maximum
from keras.backend.torch.numpy import where
from keras.utils.argument_validation import standardize_tuple


def relu(x):
    x = convert_to_tensor(x)
    return tnn.relu(x)


def relu6(x):
    x = convert_to_tensor(x)
    return tnn.relu6(x)


def sigmoid(x):
    x = convert_to_tensor(x)
    return tnn.sigmoid(x)


def tanh(x):
    x = convert_to_tensor(x)
    return tnn.tanh(x)


def softplus(x):
    x = convert_to_tensor(x)
    return tnn.softplus(x)


def softsign(x):
    x = convert_to_tensor(x)
    return tnn.softsign(x)


def silu(x, beta=1.0):
    x = convert_to_tensor(x)
    return x * sigmoid(beta * x)


def log_sigmoid(x):
    x = convert_to_tensor(x)
    return tnn.logsigmoid(x)


def leaky_relu(x, negative_slope=0.2):
    x = convert_to_tensor(x)
    return tnn.leaky_relu(x, negative_slope=negative_slope)


def hard_sigmoid(x):
    x = convert_to_tensor(x)
    return tnn.hardsigmoid(x)


def elu(x, alpha=1.0):
    x = convert_to_tensor(x)
    return tnn.elu(x, alpha)


def selu(x):
    x = convert_to_tensor(x)
    return tnn.selu(x)


def gelu(x, approximate=True):
    # TODO: torch.nn.gelu expects string approximate of `"none"` or `"tanh"`
    x = convert_to_tensor(x)
    if approximate:
        return tnn.gelu(x, approximate="tanh")
    return tnn.gelu(x)


def softmax(x, axis=-1):
    x = convert_to_tensor(x)
    if axis is None:
        # Unlike numpy, PyTorch will handle axis=None as axis=-1.
        # We need this workaround for the reduction on every dim.
        output = torch.reshape(x, [-1])
        output = tnn.softmax(output, dim=-1)
        return torch.reshape(output, x.shape)
    return tnn.softmax(x, dim=axis)


def log_softmax(x, axis=-1):
    x = convert_to_tensor(x)
    if axis is None:
        # Unlike numpy, PyTorch will handle axis=None as axis=-1.
        # We need this workaround for the reduction on every dim.
        output = torch.reshape(x, [-1])
        output = tnn.log_softmax(output, dim=-1)
        return torch.reshape(output, x.shape)
    return tnn.log_softmax(x, dim=axis)


def _compute_padding_length(
    input_length, kernel_length, stride, dilation_rate=1
):
    """Compute padding length along one dimension."""
    total_padding_length = (
        dilation_rate * (kernel_length - 1) - (input_length - 1) % stride
    )
    left_padding = total_padding_length // 2
    right_padding = (total_padding_length + 1) // 2
    return (left_padding, right_padding)


def _apply_same_padding(
    inputs, kernel_size, strides, operation_type, dilation_rate=1
):
    """Apply same padding to the input tensor.

    This function will evaluate if the padding value is compatible with torch
    functions. To avoid calling `pad()` as much as possible, which may cause
    performance or memory issues, when compatible, it does not apply the padding
    to the tensor, but returns the input tensor and the padding value to pass to
    the torch functions. If not compatible, it returns the padded tensor and 0
    as the padding value.

    Returns:
        tensor: A padded tensor or the inputs.
        padding: The padding value, ready to pass to the torch functions.
    """
    spatial_shape = inputs.shape[2:]
    num_spatial_dims = len(spatial_shape)
    padding = ()

    for i in range(num_spatial_dims):
        if operation_type == "pooling":
            padding_size = _compute_padding_length(
                spatial_shape[i], kernel_size[i], strides[i]
            )
            mode = "replicate"
        else:
            dilation_rate = standardize_tuple(
                dilation_rate, num_spatial_dims, "dilation_rate"
            )
            padding_size = _compute_padding_length(
                spatial_shape[i], kernel_size[i], strides[i], dilation_rate[i]
            )
            mode = "constant"
        padding = (padding_size,) + padding

    if all([left == right for left, right in padding]):
        return inputs, [left for left, _ in padding]

    flattened_padding = tuple(
        value for left_and_right in padding for value in left_and_right
    )
    return tnn.pad(inputs, pad=flattened_padding, mode=mode), 0


def _transpose_spatial_inputs(inputs):
    num_spatial_dims = inputs.ndim - 2
    # Torch pooling does not support `channels_last` format, so
    # we need to transpose to `channels_first` format.
    if num_spatial_dims == 1:
        inputs = torch.permute(inputs, (0, 2, 1))
    elif num_spatial_dims == 2:
        inputs = torch.permute(inputs, (0, 3, 1, 2))
    elif num_spatial_dims == 3:
        inputs = torch.permute(inputs, (0, 4, 1, 2, 3))
    else:
        raise ValueError(
            "Inputs must have ndim=3, 4 or 5, "
            "corresponding to 1D, 2D and 3D inputs. "
            f"Received input shape: {inputs.shape}."
        )
    return inputs


def _transpose_spatial_outputs(outputs):
    # Undo the tranpose in `_transpose_spatial_inputs`.
    num_spatial_dims = len(outputs.shape) - 2
    if num_spatial_dims == 1:
        outputs = torch.permute(outputs, (0, 2, 1))
    elif num_spatial_dims == 2:
        outputs = torch.permute(outputs, (0, 2, 3, 1))
    elif num_spatial_dims == 3:
        outputs = torch.permute(outputs, (0, 2, 3, 4, 1))
    return outputs


def _transpose_conv_kernel(kernel):
    # Torch requires conv kernel of format
    # `(out_channels, in_channels, spatial_dims)`, we need to transpose.
    num_spatial_dims = len(kernel.shape) - 2
    if num_spatial_dims == 1:
        kernel = torch.permute(kernel, (2, 1, 0))
    elif num_spatial_dims == 2:
        kernel = torch.permute(kernel, (3, 2, 0, 1))
    elif num_spatial_dims == 3:
        kernel = torch.permute(kernel, (4, 3, 0, 1, 2))
    return kernel


def max_pool(
    inputs,
    pool_size,
    strides=None,
    padding="valid",
    data_format=None,
):
    inputs = convert_to_tensor(inputs)
    num_spatial_dims = inputs.ndim - 2
    pool_size = standardize_tuple(pool_size, num_spatial_dims, "pool_size")
    if strides is None:
        strides = pool_size
    else:
        strides = standardize_tuple(strides, num_spatial_dims, "strides")

    data_format = standardize_data_format(data_format)
    if data_format == "channels_last":
        inputs = _transpose_spatial_inputs(inputs)

    if padding == "same":
        # Torch does not natively support `"same"` padding, we need to manually
        # apply the right amount of padding to `inputs`.
        inputs, padding = _apply_same_padding(
            inputs, pool_size, strides, operation_type="pooling"
        )
    else:
        padding = 0

    device = get_device()
    # Torch max pooling ops do not support symbolic tensors.
    # Create a real tensor to execute the ops.
    if device == "meta":
        inputs = torch.empty(
            size=inputs.shape, dtype=inputs.dtype, device="cpu"
        )

    if num_spatial_dims == 1:
        outputs = tnn.max_pool1d(
            inputs, kernel_size=pool_size, stride=strides, padding=padding
        )
    elif num_spatial_dims == 2:
        outputs = tnn.max_pool2d(
            inputs, kernel_size=pool_size, stride=strides, padding=padding
        )
    elif num_spatial_dims == 3:
        outputs = tnn.max_pool3d(
            inputs, kernel_size=pool_size, stride=strides, padding=padding
        )
    else:
        raise ValueError(
            "Inputs to pooling op must have ndim=3, 4 or 5, "
            "corresponding to 1D, 2D and 3D inputs. "
            f"Received input shape: {inputs.shape}."
        )

    outputs = outputs.to(device)
    if data_format == "channels_last":
        outputs = _transpose_spatial_outputs(outputs)
    return outputs


def average_pool(
    inputs,
    pool_size,
    strides=None,
    padding="valid",
    data_format=None,
):
    inputs = convert_to_tensor(inputs)
    num_spatial_dims = inputs.ndim - 2
    pool_size = standardize_tuple(pool_size, num_spatial_dims, "pool_size")
    if strides is None:
        strides = pool_size
    else:
        strides = standardize_tuple(strides, num_spatial_dims, "strides")

    data_format = standardize_data_format(data_format)
    if data_format == "channels_last":
        inputs = _transpose_spatial_inputs(inputs)
    padding_value = 0
    if padding == "same":
        spatial_shape = inputs.shape[2:]
        num_spatial_dims = len(spatial_shape)
        padding_value = []
        uneven_padding = []

        for i in range(num_spatial_dims):
            padding_size = _compute_padding_length(
                spatial_shape[i], pool_size[i], strides[i]
            )
            # Torch only supports even padding on each dim, to replicate the
            # behavior of "same" padding of `tf.keras` as much as possible,
            # we need to pad evenly using the shorter padding.
            padding_value.append(padding_size[0])
            if padding_size[0] != padding_size[1]:
                # Handle unequal padding.
                # `torch.nn.pad` sets padding value in the reverse order.
                uneven_padding = [0, 1] + uneven_padding
        # Only call tnn.pad when needed.
        if len(uneven_padding) > 0:
            inputs = tnn.pad(inputs, uneven_padding)

    if num_spatial_dims == 1:
        outputs = tnn.avg_pool1d(
            inputs,
            kernel_size=pool_size,
            stride=strides,
            padding=padding_value,
            count_include_pad=False,
        )
    elif num_spatial_dims == 2:
        outputs = tnn.avg_pool2d(
            inputs,
            kernel_size=pool_size,
            stride=strides,
            padding=padding_value,
            count_include_pad=False,
        )
    elif num_spatial_dims == 3:
        outputs = tnn.avg_pool3d(
            inputs,
            kernel_size=pool_size,
            stride=strides,
            padding=padding_value,
            count_include_pad=False,
        )
    else:
        raise ValueError(
            "Inputs to pooling op must have ndim=3, 4 or 5, "
            "corresponding to 1D, 2D and 3D inputs. "
            f"Received input shape: {inputs.shape}."
        )
    if data_format == "channels_last":
        outputs = _transpose_spatial_outputs(outputs)
    return outputs


def conv(
    inputs,
    kernel,
    strides=1,
    padding="valid",
    data_format=None,
    dilation_rate=1,
):
    inputs = convert_to_tensor(inputs)
    kernel = convert_to_tensor(kernel)
    num_spatial_dims = inputs.ndim - 2
    strides = standardize_tuple(strides, num_spatial_dims, "strides")

    data_format = standardize_data_format(data_format)
    if data_format == "channels_last":
        inputs = _transpose_spatial_inputs(inputs)
    # Transpose kernel from keras format to torch format.
    kernel = _transpose_conv_kernel(kernel)
    if padding == "same" and any(d != 1 for d in tree.flatten(strides)):
        # Torch does not support this case in conv2d().
        # Manually pad the tensor.
        inputs, padding = _apply_same_padding(
            inputs,
            kernel.shape[2:],
            strides,
            operation_type="conv",
            dilation_rate=dilation_rate,
        )
    channels = inputs.shape[1]
    kernel_in_channels = kernel.shape[1]
    if channels % kernel_in_channels > 0:
        raise ValueError(
            "The number of input channels must be evenly divisible by "
            f"kernel.shape[1]. Received: inputs.shape={inputs.shape}, "
            f"kernel.shape={kernel.shape}"
        )
    groups = channels // kernel_in_channels
    if num_spatial_dims == 1:
        outputs = tnn.conv1d(
            inputs,
            kernel,
            stride=strides,
            dilation=dilation_rate,
            groups=groups,
            padding=padding,
        )
    elif num_spatial_dims == 2:
        outputs = tnn.conv2d(
            inputs,
            kernel,
            stride=strides,
            dilation=dilation_rate,
            groups=groups,
            padding=padding,
        )
    elif num_spatial_dims == 3:
        outputs = tnn.conv3d(
            inputs,
            kernel,
            stride=strides,
            dilation=dilation_rate,
            groups=groups,
            padding=padding,
        )
    else:
        raise ValueError(
            "Inputs to conv operation should have ndim=3, 4, or 5,"
            "corresponding to 1D, 2D and 3D inputs. Received input "
            f"shape: {inputs.shape}."
        )

    if data_format == "channels_last":
        outputs = _transpose_spatial_outputs(outputs)
    return outputs


def depthwise_conv(
    inputs,
    kernel,
    strides=1,
    padding="valid",
    data_format=None,
    dilation_rate=1,
):
    kernel = convert_to_tensor(kernel)
    kernel = torch.reshape(
        kernel, kernel.shape[:-2] + (1, kernel.shape[-2] * kernel.shape[-1])
    )
    return conv(inputs, kernel, strides, padding, data_format, dilation_rate)


def separable_conv(
    inputs,
    depthwise_kernel,
    pointwise_kernel,
    strides=1,
    padding="valid",
    data_format=None,
    dilation_rate=1,
):
    depthwise_conv_output = depthwise_conv(
        inputs,
        depthwise_kernel,
        strides,
        padding,
        data_format,
        dilation_rate,
    )
    return conv(
        depthwise_conv_output,
        pointwise_kernel,
        strides=1,
        padding="valid",
        data_format=data_format,
        dilation_rate=dilation_rate,
    )


def conv_transpose(
    inputs,
    kernel,
    strides=1,
    padding="valid",
    output_padding=None,
    data_format=None,
    dilation_rate=1,
):
    inputs = convert_to_tensor(inputs)
    kernel = convert_to_tensor(kernel)
    num_spatial_dims = inputs.ndim - 2
    strides = standardize_tuple(strides, num_spatial_dims, "strides")

    data_format = standardize_data_format(data_format)
    (
        torch_padding,
        torch_output_padding,
    ) = compute_conv_transpose_padding_args_for_torch(
        input_shape=inputs.shape,
        kernel_shape=kernel.shape,
        strides=strides,
        padding=padding,
        output_padding=output_padding,
        dilation_rate=dilation_rate,
    )
    if data_format == "channels_last":
        inputs = _transpose_spatial_inputs(inputs)
    # Transpose kernel from keras format to torch format.
    kernel = _transpose_conv_kernel(kernel)
    kernel_spatial_shape = kernel.shape[2:]
    if isinstance(dilation_rate, int):
        dilation_rate = [dilation_rate] * len(kernel_spatial_shape)

    if num_spatial_dims == 1:
        outputs = tnn.conv_transpose1d(
            inputs,
            kernel,
            stride=strides,
            padding=torch_padding,
            output_padding=torch_output_padding,
            dilation=dilation_rate,
        )
    elif num_spatial_dims == 2:
        outputs = tnn.conv_transpose2d(
            inputs,
            kernel,
            stride=strides,
            padding=torch_padding,
            output_padding=torch_output_padding,
            dilation=dilation_rate,
        )
    elif num_spatial_dims == 3:
        outputs = tnn.conv_transpose3d(
            inputs,
            kernel,
            stride=strides,
            padding=torch_padding,
            output_padding=torch_output_padding,
            dilation=dilation_rate,
        )
    else:
        raise ValueError(
            "Inputs to conv transpose operation should have ndim=3, 4, or 5,"
            "corresponding to 1D, 2D and 3D inputs. Received input "
            f"shape: {inputs.shape}."
        )
    if data_format == "channels_last":
        outputs = _transpose_spatial_outputs(outputs)
    return outputs


def one_hot(x, num_classes, axis=-1, dtype="float32"):
    # Axis is the output axis. By default, PyTorch, outputs to last axis.
    # If axis is not last, change output to axis and shift remaining elements.
    x = convert_to_tensor(x, dtype=torch.long)

    # Torch one_hot does not natively handle negative values, so we add some
    # manual handling for negatives in the input to one_hot by using max(x, 0).
    # The output will have some invalid results, so we set them back to 0 using
    # `where` afterwards.
    output = tnn.one_hot(maximum(x, 0), num_classes)
    output = where(expand_dims(x, axis=-1) >= 0, output, 0)
    output = convert_to_tensor(output, dtype=dtype)
    dims = output.dim()
    if axis != -1 and axis != dims:
        new_axes_order = list(range(dims))
        new_axes_order[axis] = -1  # Shifts output to axis positon
        # Shift remaining axes with offset by 1 since output moved to `axis`.
        for ax in range(axis + 1, dims):
            new_axes_order[ax] -= 1
        output = output.permute(new_axes_order)
    return output


def multi_hot(x, num_classes, axis=-1, dtype="float32"):
    reduction_axis = 1 if len(x.shape) > 1 else 0
    outputs = torch.amax(
        one_hot(cast(x, "int32"), num_classes, axis=axis, dtype=dtype),
        dim=reduction_axis,
    )
    return outputs


def categorical_crossentropy(target, output, from_logits=False, axis=-1):
    target = convert_to_tensor(target)
    output = convert_to_tensor(output)

    if target.shape != output.shape:
        raise ValueError(
            "Arguments `target` and `output` must have the same shape. "
            "Received: "
            f"target.shape={target.shape}, output.shape={output.shape}"
        )
    if len(target.shape) < 1:
        raise ValueError(
            "Arguments `target` and `output` must be at least rank 1. "
            "Received: "
            f"target.shape={target.shape}, output.shape={output.shape}"
        )

    if from_logits:
        log_prob = tnn.log_softmax(output, dim=axis)
    else:
        output = output / torch.sum(output, dim=axis, keepdim=True)
        output = torch.clip(output, epsilon(), 1.0 - epsilon())
        log_prob = torch.log(output)
    return -torch.sum(target * log_prob, dim=axis)


def sparse_categorical_crossentropy(target, output, from_logits=False, axis=-1):
    target = convert_to_tensor(target, dtype=torch.long)
    output = convert_to_tensor(output)

    if len(target.shape) == len(output.shape) and target.shape[-1] == 1:
        target = torch.squeeze(target, dim=-1)

    if len(output.shape) < 1:
        raise ValueError(
            "Argument `output` must be at least rank 1. "
            "Received: "
            f"output.shape={output.shape}"
        )
    if target.shape != output.shape[:-1]:
        raise ValueError(
            "Arguments `target` and `output` must have the same shape "
            "up until the last dimension: "
            f"target.shape={target.shape}, output.shape={output.shape}"
        )
    if from_logits:
        log_prob = tnn.log_softmax(output, dim=axis)
    else:
        output = output / torch.sum(output, dim=axis, keepdim=True)
        output = torch.clip(output, epsilon(), 1.0 - epsilon())
        log_prob = torch.log(output)
    target = one_hot(target, output.shape[axis], axis=axis)
    return -torch.sum(target * log_prob, dim=axis)


def binary_crossentropy(target, output, from_logits=False):
    target = convert_to_tensor(target)
    output = convert_to_tensor(output)

    if target.shape != output.shape:
        raise ValueError(
            "Arguments `target` and `output` must have the same shape. "
            "Received: "
            f"target.shape={target.shape}, output.shape={output.shape}"
        )
    # By default, PyTorch, does reduction of `sum` over all rows,
    # change reduction to `none` to keep dim
    if from_logits:
        return tnn.binary_cross_entropy_with_logits(
            output, target, reduction="none"
        )
    else:
        output = torch.clip(output, epsilon(), 1.0 - epsilon())
        return tnn.binary_cross_entropy(output, target, reduction="none")


def moments(x, axes, keepdims=False, synchronized=False):
    if synchronized:
        raise NotImplementedError(
            "Argument synchronized=True is not supported with PyTorch."
        )
    x = convert_to_tensor(x)
    # The dynamic range of float16 is too limited for statistics. As a
    # workaround, we simply perform the operations on float32 and convert back
    # to float16
    need_cast = False
    ori_dtype = standardize_dtype(x.dtype)
    if ori_dtype == "float16":
        need_cast = True
        x = cast(x, "float32")

    mean = torch.mean(x, dim=axes, keepdim=True)

    # The variance is computed using $Var = E[|x|^2] - |E[x]|^2$, It is faster
    # but less numerically stable.
    # Note: stop_gradient does not change the gradient to the mean, because that
    # gradient is zero.
    variance = torch.mean(
        torch.square(x), dim=axes, keepdim=True
    ) - torch.square(mean.detach())

    if not keepdims:
        mean = torch.squeeze(mean, axes)
        variance = torch.squeeze(variance, axes)
    if need_cast:
        # avoid overflow and underflow when casting from float16 to float32
        mean = torch.clip(
            mean,
            torch.finfo(torch.float16).min,
            torch.finfo(torch.float16).max,
        )
        variance = torch.clip(
            variance,
            torch.finfo(torch.float16).min,
            torch.finfo(torch.float16).max,
        )
        mean = cast(mean, ori_dtype)
        variance = cast(variance, ori_dtype)
    return mean, variance
