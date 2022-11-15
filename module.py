from collections import OrderedDict, namedtuple
import itertools
import warnings
import functools

import torch
from ..parameter import Parameter
import torch.utils.hooks as hooks

from torch import Tensor, device, dtype
from typing import Union, Tuple, Any, Callable, Iterator, Set, Optional, overload, TypeVar, Mapping, Dict, List
from ...utils.hooks import RemovableHandle

_grad_t = Union[Tuple[Tensor, ...], Tensor]
# See https://mypy.readthedocs.io/en/latest/generics.html#generic-methods-and-generic-self for the use
# of `T` to annotate `self`. Many methods of `Module` return `self` and we want those return values to be
# the type of the subclass, not the looser type of `Module`.
T = TypeVar('T', bound='Module')


class _IncompatibleKeys(namedtuple('IncompatibleKeys', ['missing_keys', 'unexpected_keys'])):
    def __repr__(self):
        if not self.missing_keys and not self.unexpected_keys:
            return '<All keys matched successfully>'
        return super(_IncompatibleKeys, self).__repr__()

    __str__ = __repr__


def _addindent(s_, numSpaces):
    s = s_.split('\n')
    # don't do anything for single-line stuff
    if len(s) == 1:
        return s_
    first = s.pop(0)
    s = [(numSpaces * ' ') + line for line in s]
    s = '\n'.join(s)
    s = first + '\n' + s
    return s


r"""This tracks hooks common to all modules that are executed before/after
calling forward and backward. This is global state used for debugging/profiling
purposes"""
_global_backward_hooks: Dict[int, Callable] = OrderedDict()
_global_is_full_backward_hook: Optional[bool] = None
_global_forward_pre_hooks: Dict[int, Callable] = OrderedDict()
_global_forward_hooks: Dict[int, Callable] = OrderedDict()

_EXTRA_STATE_KEY_SUFFIX = '_extra_state'


def register_module_forward_pre_hook(hook: Callable[..., None]) -> RemovableHandle:
    r"""Registers a forward pre-hook common to all modules.

    .. warning ::

        This adds global state to the `nn.module` module
        and it is only intended for debugging/profiling purposes.

    The hook will be called every time before :func:`forward` is invoked.
    It should have the following signature::

        hook(module, input) -> None or modified input

    The input contains only the positional arguments given to the module.
    Keyword arguments won't be passed to the hooks and only to the ``forward``.
    The hook can modify the input. User can either return a tuple or a
    single modified value in the hook. We will wrap the value into a tuple
    if a single value is returned(unless that value is already a tuple).

    This hook has precedence over the specific module hooks registered with
    ``register_forward_pre_hook``.

    Returns:
        :class:`torch.utils.hooks.RemovableHandle`:
            a handle that can be used to remove the added hook by calling
            ``handle.remove()``
    """
    handle = hooks.RemovableHandle(_global_forward_pre_hooks)
    _global_forward_pre_hooks[handle.id] = hook
    return handle


def register_module_forward_hook(hook: Callable[..., None]) -> RemovableHandle:
    r"""Registers a global forward hook for all the modules

    .. warning ::

        This adds global state to the `nn.module` module
        and it is only intended for debugging/profiling purposes.

    The hook will be called every time after :func:`forward` has computed an output.
    It should have the following signature::

        hook(module, input, output) -> None or modified output

    The input contains only the positional arguments given to the module.
    Keyword arguments won't be passed to the hooks and only to the ``forward``.
    The hook can modify the output. It can modify the input inplace but
    it will not have effect on forward since this is called after
    :func:`forward` is called.

    Returns:
        :class:`torch.utils.hooks.RemovableHandle`:
            a handle that can be used to remove the added hook by calling
            ``handle.remove()``

    This hook will be executed before specific module hooks registered with
    ``register_forward_hook``.
    """
    handle = hooks.RemovableHandle(_global_forward_hooks)
    _global_forward_hooks[handle.id] = hook
    return handle


def register_module_backward_hook(
        hook: Callable[['Module', _grad_t, _grad_t], Union[None, Tensor]]
) -> RemovableHandle:
    r"""Registers a backward hook common to all the modules.

    This function is deprecated in favor of
    :func:`torch.nn.modules.module.register_module_full_backward_hook`
    and the behavior of this function will change in future versions.

    Returns:
        :class:`torch.utils.hooks.RemovableHandle`:
            a handle that can be used to remove the added hook by calling
            ``handle.remove()``

    """
    global _global_is_full_backward_hook
    if _global_is_full_backward_hook is True:
        raise RuntimeError("Cannot use both regular backward hooks and full backward hooks as a "
                           "global Module hook. Please use only one of them.")

    _global_is_full_backward_hook = False

    handle = hooks.RemovableHandle(_global_backward_hooks)
    _global_backward_hooks[handle.id] = hook
    return handle


def register_module_full_backward_hook(
        hook: Callable[['Module', _grad_t, _grad_t], Union[None, Tensor]]
) -> RemovableHandle:
    r"""Registers a backward hook common to all the modules.

    .. warning ::
        This adds global state to the `nn.module` module
        and it is only intended for debugging/profiling purposes.

    The hook will be called every time the gradients with respect to module
    inputs are computed. The hook should have the following signature::

        hook(module, grad_input, grad_output) -> Tensor or None

    The :attr:`grad_input` and :attr:`grad_output` are tuples. The hook should
    not modify its arguments, but it can optionally return a new gradient with
    respect to the input that will be used in place of :attr:`grad_input` in
    subsequent computations. :attr:`grad_input` will only correspond to the inputs given
    as positional arguments and all kwarg arguments will not appear in the hook. Entries
    in :attr:`grad_input` and :attr:`grad_output` will be ``None`` for all non-Tensor
    arguments.

    For technical reasons, when this hook is applied to a Module, its forward function will
    receive a view of each Tensor passed to the Module. Similarly the caller will receive a view
    of each Tensor returned by the Module's forward function.

    Global hooks are called before hooks registered with `register_backward_hook`

    Returns:
        :class:`torch.utils.hooks.RemovableHandle`:
            a handle that can be used to remove the added hook by calling
            ``handle.remove()``

    """
    global _global_is_full_backward_hook
    if _global_is_full_backward_hook is False:
        raise RuntimeError("Cannot use both regular backward hooks and full backward hooks as a "
                           "global Module hook. Please use only one of them.")

    _global_is_full_backward_hook = True

    handle = hooks.RemovableHandle(_global_backward_hooks)
    _global_backward_hooks[handle.id] = hook
    return handle


# Trick mypy into not applying contravariance rules to inputs by defining
# forward as a value, rather than a function.  See also
# https://github.com/python/mypy/issues/8795
def _forward_unimplemented(self, *input: Any) -> None:
    r"""Defines the computation performed at every call.

    Should be overridden by all subclasses.

    .. note::
        Although the recipe for forward pass needs to be defined within
        this function, one should call the :class:`Module` instance afterwards
        instead of this since the former takes care of running the
        registered hooks while the latter silently ignores them.
    """
    raise NotImplementedError(f"Module [{type(self).__name__}] is missing the required \"forward\" function")


class Module:
    r"""Base class for all neural network modules.
    模块(Module）是所有神经网络模型的基类。

    Your models should also subclass this class.
    你创建模型的时候也应该继承这个类

    Modules can also contain other Modules, allowing to nest them in
    a tree structure. You can assign the submodules as regular attributes::
    模块(Module)中还可以包含其他的模块，嵌套其他的模块组织成一个树结构
    可以将一个模块赋值成为另一个模块的属性，从而成为这个模块的一个子模块。

        import torch.nn as nn
        import torch.nn.functional as F

        class Model(nn.Module):
            def __init__(self):
                super().__init__()
                self.conv1 = nn.Conv2d(1, 20, 5)
                self.conv2 = nn.Conv2d(20, 20, 5)

            def forward(self, x):
                x = F.relu(self.conv1(x))
                return F.relu(self.conv2(x))

    Submodules assigned in this way will be registered, and will have their
    parameters converted too when you call :meth:`to`, etc.
    通过赋值这种方式添加的子模块将会被模型注册(register)，
    而后当调用模块的一些参数转换函数(to()）的时候，子模块的参数也会一并转换。

    .. note::
        As per the example above, an ``__init__()`` call to the parent class
        must be made before assignment on the child.
        根据上面的例子，继承父类__init__()初始化方法必须在赋值子类之前

    :ivar training: Boolean represents whether this module is in training or
                    evaluation mode.
                    布尔值表示此模块无论是在训练模式还是在评估模式

    :vartype training: bool
    """

    dump_patches: bool = False

    _version: int = 1
    r"""This allows better BC support for :meth:`load_state_dict`. In
    :meth:`state_dict`, the version number will be saved as in the attribute
    `_metadata` of the returned state dict, and thus pickled. `_metadata` is a
    dictionary with keys that follow the naming convention of state dict. See
    ``_load_from_state_dict`` on how to use this information in loading.

    If new parameters/buffers are added/removed from a module, this number shall
    be bumped, and the module's `_load_from_state_dict` method can compare the
    version number and do appropriate changes if the state dict is from before
    the change."""

    training: bool
    _is_full_backward_hook: Optional[bool]

    """
    继承 nn.Module 的神经网络模块在实现自己的 __init__ 函数时，一定要先调用 super().__init__()。
    只有这样才能正确地初始化自定义的神经网络模块，否则会缺少上面代码中的成员变量而导致模块被调用时出错。
    实际上，如果没有提前调用 super().__init__()，在增加模块的 parameter 或者 buffer 的时候，
    被调用的 __setattr__ 函数也会检查出父类 nn.Module 没被正确地初始化并报错。
    """

    def __init__(self) -> None:
        """
        Initializes internal Module state, shared by both nn.Module and ScriptModule.
        初始化由 nn.Module 和 ScriptModule 共享的内部模块状态
        """
        torch._C._log_api_usage_once("python.nn_module")

        self.training = True
        self._parameters: Dict[str, Optional[Parameter]] = OrderedDict()
        self._buffers: Dict[str, Optional[Tensor]] = OrderedDict()
        self._non_persistent_buffers_set: Set[str] = set()
        self._backward_hooks: Dict[int, Callable] = OrderedDict()
        self._is_full_backward_hook = None
        self._forward_hooks: Dict[int, Callable] = OrderedDict()
        self._forward_pre_hooks: Dict[int, Callable] = OrderedDict()
        self._state_dict_hooks: Dict[int, Callable] = OrderedDict()
        self._load_state_dict_pre_hooks: Dict[int, Callable] = OrderedDict()
        self._load_state_dict_post_hooks: Dict[int, Callable] = OrderedDict()
        self._modules: Dict[str, Optional['Module']] = OrderedDict()

    forward: Callable[..., Any] = _forward_unimplemented

    """
        属性设置
    
        对 nn.Module 属性的修改有以下三个函数，函数以及对应功能如下
    
        1、add_module：增加子神经网络模块，更新 self._modules
        2、register_parameter：增加通过 BP 可以更新的 parameters （如 BN 和 Conv 中的 weight 和 bias ），更新 self._parameters
        3、register_buffer：增加不通过 BP 更新的 buffer（如 BN 中的 running_mean 和 running_var），
            更新 self._buffers，如果 buffer 不是 persistant 的，还会同时更新到 self._non_persistent_buffers_set 中。
            buffer 是否 persistant 的区别在于这个 buffer 是否会能被放入 self.state_dict 中被保存下来。 
            这 3 个函数都会先检查 self.__dict__ 中是否包含对应的属性字典以确保 nn.Module 被正确初始化，然后检查属性的 name 是否合法，
            如不为空 string 且不包含“.”，同时还会检查他们是否已经存在于要修改的属性字典中。
        4、在日常的代码开发过程中，更常见的用法是直接通过 self.xxx = xxx 的方式来增加或修改子神经网络模块、parameters、
            buffers 以及其他一般的 attribute。这种方式本质上会调用 nn.Module 重载的函数 __setattr__ 

    """
    def register_buffer(self, name: str, tensor: Optional[Tensor], persistent: bool = True) -> None:
        """
        增加不通过 BP 更新的 buffer（如 BN 中的 running_mean 和 running_var），
        更新 self._buffers，如果 buffer 不是 persistant 的，还会同时更新到 self._non_persistent_buffers_set 中。
        buffer 是否 persistant 的区别在于这个 buffer 是否会能被放入 self.state_dict 中被保存下来。

        """

        r"""Adds a buffer to the module.
            添加一个buffer(缓冲区)到module

        This is typically used to register a buffer that should not to be
        considered a model parameter. For example, BatchNorm's ``running_mean``
        is not a parameter, but is part of the module's state. Buffers, by
        default, are persistent and will be saved alongside parameters. This
        behavior can be changed by setting :attr:`persistent` to ``False``. The
        only difference between a persistent buffer and a non-persistent buffer
        is that the latter will not be a part of this module's
        :attr:`state_dict`.
        注册一个缓冲区，这个buffer(缓冲区)不是一个模型参数。
        例如，BatchNorm's ``running_mean``不是一个参数，仅仅是模型状态的一部分。
        默认情况，Buffers(缓冲区)持久存在，并且将和parameters（参数）一起保存。
        这个情况，可以被改变通过设置参数"persistent=False"。
        持久性缓冲区和非持久性缓冲区之间的唯一区别是后者将不保存在这个模块attr:`state_dict`中。


        Buffers can be accessed as attributes using given names.
        可以使用给定的名称作为属性访问缓冲区。

        Args:
            name (string): name of the buffer. The buffer can be accessed
                from this module using the given name
                缓冲区的名称。使用给定的名称访问该模块的缓冲区
            tensor (Tensor or None): buffer to be registered. If ``None``, then operations
                that run on buffers, such as :attr:`cuda`, are ignored. If ``None``,
                the buffer is **not** included in the module's :attr:`state_dict`.

            persistent (bool): whether the buffer is part of this module's
                :attr:`state_dict`.
                buffer(缓冲区)是否需要保存在这个模块的attr:`state_dict`中。

        Example::

            >>> self.register_buffer('running_mean', torch.zeros(num_features))

        """
        if persistent is False and isinstance(self, torch.jit.ScriptModule):
            raise RuntimeError("ScriptModule does not support non-persistent buffers")

        if '_buffers' not in self.__dict__:
            raise AttributeError(
                "cannot assign buffer before Module.__init__() call")
            # 在调用 Module. _ _ init _ _ ()之前无法分配缓冲区
        elif not isinstance(name, torch._six.string_classes):
            raise TypeError("buffer name should be a string. "
                            "Got {}".format(torch.typename(name)))
        elif '.' in name:
            raise KeyError("buffer name can't contain \".\"")
        elif name == '':
            raise KeyError("buffer name can't be empty string \"\"")
        elif hasattr(self, name) and name not in self._buffers:
            raise KeyError("attribute '{}' already exists".format(name))
        elif tensor is not None and not isinstance(tensor, torch.Tensor):
            raise TypeError("cannot assign '{}' object to buffer '{}' "
                            "(torch Tensor or None required)"
                            .format(torch.typename(tensor), name))
        else:
            self._buffers[name] = tensor
            # 如果 buffer 不是 persistant 的，还会同时更新到 self._non_persistent_buffers_set 中
            if persistent:
                self._non_persistent_buffers_set.discard(name)
            else:
                self._non_persistent_buffers_set.add(name)

    def register_parameter(self, name: str, param: Optional[Parameter]) -> None:
        r"""Adds a parameter to the module.
            向模块添加一个参数(增加通过 BP 可以更新的parameters（如 BN 和 Conv 中的 weight 和 bias ），更新 self._parameters）

        The parameter can be accessed as an attribute using given name.
        所添加的参数(parameter）可以通过给定的名字(name参数)以访问模块的属性的方式进行访问

        Args:
            name (string): name of the parameter. The parameter can be accessed
                from this module using the given name
                所添加的参数的名字. 所添加的参数(parameter）可以通过此名字以访问模块的属性的方式进行访问

            param (Parameter or None): parameter to be added to the module. If
                ``None``, then operations that run on parameters, such as :attr:`cuda`,
                are ignored. If ``None``, the parameter is **not** included in the
                module's :attr:`state_dict`.
        """
        if '_parameters' not in self.__dict__:
            raise AttributeError(
                "cannot assign parameter before Module.__init__() call")

        elif not isinstance(name, torch._six.string_classes):
            raise TypeError("parameter name should be a string. "
                            "Got {}".format(torch.typename(name)))
        elif '.' in name:
            raise KeyError("parameter name can't contain \".\"")
        elif name == '':
            raise KeyError("parameter name can't be empty string \"\"")
        elif hasattr(self, name) and name not in self._parameters:
            raise KeyError("attribute '{}' already exists".format(name))

        if param is None:
            self._parameters[name] = None
        elif not isinstance(param, Parameter):
            raise TypeError("cannot assign '{}' object to parameter '{}' "
                            "(torch.nn.Parameter or None required)"
                            .format(torch.typename(param), name))
        elif param.grad_fn:
            # grad_fn用来记录变量是怎么来的，方便计算梯度。如，y = x*3, grad_fn记录了y由x计算的过程。
            # 如果param.grad_fn=True，说明参数是一个中间节点，不是一个叶节点
            raise ValueError(
                "Cannot assign non-leaf Tensor to parameter '{0}'. Model "
                "parameters must be created explicitly. To express '{0}' "
                "as a function of another Tensor, compute the value in "
                "the forward() method.".format(name))

        else:
            self._parameters[name] = param

    def add_module(self, name: str, module: Optional['Module']) -> None:
        r"""Adds a child module to the current module.
            向当前模块添加一个子模块。（增加子神经网络模块，更新 self._modules）

        The module can be accessed as an attribute using the given name.
        此子模块可以作为当前模块的属性被访问到，而属性名就是add_module()函数中的name参数。

        Args:
            name (string): name of the child module. The child module can be
                accessed from this module using the given name
            module (Module): child module to be added to the module.
        """
        if not isinstance(module, Module) and module is not None:
            raise TypeError("{} is not a Module subclass".format(
                torch.typename(module)))
        elif not isinstance(name, torch._six.string_classes):
            raise TypeError("module name should be a string. Got {}".format(
                torch.typename(name)))
        elif hasattr(self, name) and name not in self._modules:
            raise KeyError("attribute '{}' already exists".format(name))
        elif '.' in name:
            raise KeyError("module name can't contain \".\", got: {}".format(name))
        elif name == '':
            raise KeyError("module name can't be empty string \"\"")
        self._modules[name] = module

    def register_module(self, name: str, module: Optional['Module']) -> None:
        r"""Alias for :func:`add_module`."""
        self.add_module(name, module)

    def get_submodule(self, target: str) -> "Module":
        """
        Returns the submodule given by ``target`` if it exists,
        otherwise throws an error.
        如果target给出的子模块存在，则返回子模块，否则抛出错误

        For example, let's say you have an ``nn.Module`` ``A`` that
        looks like this:

        .. code-block:: text

            A(
                (net_b): Module(
                    (net_c): Module(
                        (conv): Conv2d(16, 33, kernel_size=(3, 3), stride=(2, 2))
                    )
                    (linear): Linear(in_features=100, out_features=200, bias=True)
                )
            )

        (The diagram shows an ``nn.Module`` ``A``. ``A`` has a nested（嵌套）
        submodule ``net_b``, which itself has two submodules ``net_c``
        and ``linear``. ``net_c`` then has a submodule ``conv``.)

        To check whether or not we have the ``linear`` submodule, we
        would call ``get_submodule("net_b.linear")``. To check whether
        we have the ``conv`` submodule, we would call
        ``get_submodule("net_b.net_c.conv")``.

        The runtime of ``get_submodule`` is bounded by the degree
        of module nesting in ``target``. A query against
        ``named_modules`` achieves the same result, but it is O(N) in
        the number of transitive modules. So, for a simple check to see
        if some submodule exists, ``get_submodule`` should always be
        used.

        Args:
            target: The fully-qualified string name of the submodule
                to look for. (See above example for how to specify a
                fully-qualified string.)
                查找全路径的子模块字符串名称。
                如上述例子中的``get_submodule("net_b.linear")``和``get_submodule("net_b.net_c.conv")``

        Returns:
            torch.nn.Module: The submodule referenced by ``target``
            被target引用的子模块,Return type:torch.nn.Modules

        Raises:
            AttributeError: If the target string references an invalid
                path or resolves to something that is not an
                ``nn.Module``
                如果目标字符串引用了无效的路径或解析到不是nn.Module
        """
        if target == "":
            return self

        atoms: List[str] = target.split(".")
        mod: torch.nn.Module = self

        for item in atoms:

            if not hasattr(mod, item):  # 判断item是否是父模块的子模块
                raise AttributeError(mod._get_name() + " has no "
                                                       "attribute `" + item + "`")

            mod = getattr(mod, item)  # 获取对应子模块名称的子模块

            if not isinstance(mod, torch.nn.Module):  # 判断mod是否继承自nn.Moudle
                raise AttributeError("`" + item + "` is not "
                                                  "an nn.Module")

        return mod

    def get_parameter(self, target: str) -> "Parameter":
        """
        Returns the parameter given by ``target`` if it exists,
        otherwise throws an error.
        如果target指定的参数存在，则返回该参数，否则抛出错误。

        See the docstring for ``get_submodule`` for a more detailed
        explanation of this method's functionality as well as how to
        correctly specify ``target``.
        有关此方法的功能以及如何正确指定目标的更详细的解释，请参阅get_submodule的文档字符串。

        Args:
            target: The fully-qualified string name of the Parameter
                to look for. (See ``get_submodule`` for how to specify a
                fully-qualified string.)
            要查找的参数的全限定字符串名称。(参见get_submodule了解如何指定全限定字符串。)

        Returns:
            torch.nn.Parameter: The Parameter referenced by ``target``
            目标引用的参数, Return type:torch.nn.Parameter

        Raises:
            AttributeError: If the target string references an invalid
                path or resolves to something that is not an
                ``nn.Parameter``
            如果目标字符串引用了无效的路径或解析到不是nn.Parameter
        """
        # 分割全限定字符串名称,target = "net_b.net_c.conv" >> ('net_b.net_c', '.', 'conv')
        module_path, _, param_name = target.rpartition(".")

        # 调用get_submodule()获取最底层子模块
        mod: torch.nn.Module = self.get_submodule(module_path)

        if not hasattr(mod, param_name):  # 判断该子模块是否包含param_name名称
            raise AttributeError(mod._get_name() + " has no attribute `"
                                 + param_name + "`")

        # 获取模块mod的属性名称为param_name的参数值
        param: torch.nn.Parameter = getattr(mod, param_name)

        if not isinstance(param, torch.nn.Parameter):
            raise AttributeError("`" + param_name + "` is not an "
                                                    "nn.Parameter")

        return param

    def get_buffer(self, target: str) -> "Tensor":
        """
        Returns the buffer given by ``target`` if it exists,
        otherwise throws an error.
        如果存在，返回target给出的缓冲区，否则抛出错误。

        See the docstring for ``get_submodule`` for a more detailed
        explanation of this method's functionality as well as how to
        correctly specify ``target``.
        有关此方法的功能以及如何正确指定目标的更详细的解释，请参阅get_submodule的文档字符串。

        Args:
            target: The fully-qualified string name of the buffer
                to look for. (See ``get_submodule`` for how to specify a
                fully-qualified string.)
            要查找的缓冲区的全限定字符串名称。(参见get_submodule了解如何指定全限定字符串。)

        Returns:
            torch.Tensor: The buffer referenced by ``target``
            目标引用的缓冲区, Return type:torch.Tensor

        Raises:
            AttributeError: If the target string references an invalid
                path or resolves to something that is not a
                buffer
            如果目标字符串引用了无效的路径或解析为非缓冲区的内容
        """
        module_path, _, buffer_name = target.rpartition(".")

        mod: torch.nn.Module = self.get_submodule(module_path)

        if not hasattr(mod, buffer_name):
            raise AttributeError(mod._get_name() + " has no attribute `"
                                 + buffer_name + "`")

        buffer: torch.Tensor = getattr(mod, buffer_name)

        if buffer_name not in mod._buffers:
            raise AttributeError("`" + buffer_name + "` is not a buffer")

        return buffer

    def get_extra_state(self) -> Any:
        """
        Returns any extra state to include in the module's state_dict.
        Implement this and a corresponding :func:`set_extra_state` for your module
        if you need to store extra state. This function is called when building the
        module's `state_dict()`.

        Note that extra state should be pickleable to ensure working serialization
        of the state_dict. We only provide provide backwards compatibility guarantees
        for serializing Tensors; other objects may break backwards compatibility if
        their serialized pickled form changes.

        Returns:
            object: Any extra state to store in the module's state_dict
        """
        raise RuntimeError(
            "Reached a code path in Module.get_extra_state() that should never be called. "
            "Please file an issue at https://github.com/pytorch/pytorch/issues/new?template=bug-report.yml "
            "to report this bug.")

    def set_extra_state(self, state: Any):
        """
        This function is called from :func:`load_state_dict` to handle any extra state
        found within the `state_dict`. Implement this function and a corresponding
        :func:`get_extra_state` for your module if you need to store extra state within its
        `state_dict`.

        Args:
            state (dict): Extra state from the `state_dict`
        """
        raise RuntimeError(
            "Reached a code path in Module.set_extra_state() that should never be called. "
            "Please file an issue at https://github.com/pytorch/pytorch/issues/new?template=bug-report.yml "
            "to report this bug.")

    def _apply(self, fn):
        """
            def cpu()和def GPU()都是通过 self._apply(function) 配合def to()方法来实现的，
            function 一般是 lambda 表达式或其他自定义函数。因此，用户其实也可以通过 self._apply(function) 来实现一些特殊的转换。
            self._apply() 函数实际上做了如下 3 件事情，最终将 function 完整地应用于整个模块。
                1、通过 self.children() 进行递归的调用
                2、对 self._parameters 中的参数及其 gradient 通过 function 进行处理
                3、对 self._buffers 中的 buffer 逐个通过 function 来进行处理

            apply 函数和 _apply 函数的区别在于，_apply() 是专门针对 parameter 和 buffer 而实现的一个“仅供内部使用”的接口，
            但是 apply 函数是“公有”接口 。apply 实际上可以通过修改 fn 来实现 _apply 能实现的功能，同时还可以实现其他功能。
            self._apply(lambda t: t.cuda(device))
        """
        for module in self.children():
            module._apply(fn) # 对子模块调用fn方法

        def compute_should_use_set_data(tensor, tensor_applied):
            if torch._has_compatible_shallow_copy_type(tensor, tensor_applied):
                # If the new tensor has compatible tensor type as the existing tensor,
                # the current behavior is to change the tensor in-place using `.data =`,
                # and the future behavior is to overwrite the existing tensor. However,
                # changing the current behavior is a BC-breaking change, and we want it
                # to happen in future releases. So for now we introduce the
                # `torch.__future__.get_overwrite_module_params_on_conversion()`
                # global flag to let the user control whether they want the future
                # behavior of overwriting the existing tensor or not.
                return not torch.__future__.get_overwrite_module_params_on_conversion()
            else:
                return False

        for key, param in self._parameters.items():
            if param is None:
                continue
            # Tensors stored in modules are graph leaves, and we don't want to
            # track autograd history of `param_applied`, so we have to use
            # `with torch.no_grad():`
            #
            with torch.no_grad():
                param_applied = fn(param)
            should_use_set_data = compute_should_use_set_data(param, param_applied)
            if should_use_set_data:
                param.data = param_applied
                out_param = param
            else:
                assert isinstance(param, Parameter)
                assert param.is_leaf
                out_param = Parameter(param_applied, param.requires_grad)
                self._parameters[key] = out_param

            if param.grad is not None:
                with torch.no_grad():
                    grad_applied = fn(param.grad)
                should_use_set_data = compute_should_use_set_data(param.grad, grad_applied)
                if should_use_set_data:
                    out_param.grad.data = grad_applied
                else:
                    assert param.grad.is_leaf
                    out_param.grad = grad_applied.requires_grad_(param.grad.requires_grad)

        for key, buf in self._buffers.items():
            if buf is not None:
                self._buffers[key] = fn(buf)

        return self

    """
        apply 函数 与 _apply() 函数不同的是，apply 函数只是简单地递归调用了 self.children() 去处理自己以及子模块.
    """
    def apply(self: T, fn: Callable[['Module'], None]) -> T:
        r"""Applies ``fn`` recursively to every submodule (as returned by ``.children()``)
        as well as self. Typical use includes initializing the parameters of a model
        (see also :ref:`nn-init-doc`).
        递归地将fn函数应用于每个子模块(由.children()返回) 和 self。典型的用法包括初始化模型的参数(参见torch.nn.init)。

        Args:
            fn (:class:`Module` -> None): function to be applied to each submodule
            用于每个子模块的函数，中间的参数必然是子模块！如果用于参数的初始化，那么就递归该模块下的各个函数

        Returns:
            Module: self

        Example::
            经常用于初始化init_weights的操作
            >>> @torch.no_grad()
            >>> def init_weights(m):
            >>>     print(m)
            >>>     if type(m) == nn.Linear:
            >>>         m.weight.fill_(1.0)
            >>>         print(m.weight)
            >>> net = nn.Sequential(nn.Linear(2, 2), nn.Linear(2, 2))
            >>> net.apply(init_weights)
            Linear(in_features=2, out_features=2, bias=True)
            Parameter containing:
            tensor([[ 1.,  1.],
                    [ 1.,  1.]])
            Linear(in_features=2, out_features=2, bias=True)
            Parameter containing:
            tensor([[ 1.,  1.],
                    [ 1.,  1.]])
            Sequential(
              (0): Linear(in_features=2, out_features=2, bias=True)
              (1): Linear(in_features=2, out_features=2, bias=True)
            )
            Sequential(
              (0): Linear(in_features=2, out_features=2, bias=True)
              (1): Linear(in_features=2, out_features=2, bias=True)
            )
        """
        for module in self.children():
            module.apply(fn) # 对该模块的子模块调用fn方法
        fn(self) # 对当前模型调用fn方法
        return self

    """
        参数的转换或转移
    
        nn.Module 实现了如下 8 个常用函数将模块转变成 float16 等类型、转移到 CPU/ GPU上。
            1、CPU：将所有 parameters 和 buffer 转移到 CPU 上
            2、type：将所有 parameters 和 buffer 转变成另一个类型
            3、CUDA：将所有 parameters 和 buffer 转移到 GPU 上
            4、float：将所有浮点类型的 parameters 和 buffer 转变成 float32 类型
            5、double：将所有浮点类型的 parameters 和 buffer 转变成 double 类型
            6、half：将所有浮点类型的 parameters 和 buffer 转变成 float16 类型
            7、bfloat16：将所有浮点类型的 parameters 和 buffer 转变成 bfloat16 类型
            8、to：移动模块或/和改变模块的类型
        
        这些函数的功能最终都是通过 self._apply(function) 来实现的， function 一般是 lambda 表达式或其他自定义函数。
        因此，用户其实也可以通过 self._apply(function) 来实现一些特殊的转换。self._apply() 函数实际上做了如下 3 件事情，最终将 function 完整地应用于整个模块。
            1、通过 self.children() 进行递归的调用
            2、对 self._parameters 中的参数及其 gradient 通过 function 进行处理
            3、对 self._buffers 中的 buffer 逐个通过 function 来进行处理
    
    """
    def cuda(self: T, device: Optional[Union[int, device]] = None) -> T:
        r"""Moves all model parameters and buffers to the GPU.
        将所有模型参数和缓冲区移动到GPU。

        This also makes associated parameters and buffers different objects. So
        it should be called before constructing optimizer if the module will
        live on GPU while being optimized.
        这也使得相关的参数和缓冲区成为不同的对象。因此，如果模块将在GPU上运行而被优化，则应该在构建优化器之前调用它。

        .. note::
            This method modifies the module in-place.
            同样的，此方法将就地修改模块。

        Args:
            device (int, optional): if specified, all parameters will be
                copied to that device
            如果指定了，所有参数将被复制到该设备

        Returns:
            Module: self
        """
        return self._apply(lambda t: t.cuda(device))

    def ipu(self: T, device: Optional[Union[int, device]] = None) -> T:
        r"""Moves all model parameters and buffers to the IPU.

        This also makes associated parameters and buffers different objects. So
        it should be called before constructing optimizer if the module will
        live on IPU while being optimized.

        .. note::
            This method modifies the module in-place.

        Arguments:
            device (int, optional): if specified, all parameters will be
                copied to that device

        Returns:
            Module: self
        """
        return self._apply(lambda t: t.ipu(device))

    def xpu(self: T, device: Optional[Union[int, device]] = None) -> T:
        r"""Moves all model parameters and buffers to the XPU.

        This also makes associated parameters and buffers different objects. So
        it should be called before constructing optimizer if the module will
        live on XPU while being optimized.

        .. note::
            This method modifies the module in-place.

        Arguments:
            device (int, optional): if specified, all parameters will be
                copied to that device

        Returns:
            Module: self
        """
        return self._apply(lambda t: t.xpu(device))

    def cpu(self: T) -> T:
        r"""Moves all model parameters and buffers to the CPU.
        将所有模型参数和缓冲区移动到CPU。

        .. note::
            This method modifies the module in-place.
            此方法将就地修改模块（应该当前代码之前的所有立刻移动到别处）。

        Returns:
            Module: self
        """
        return self._apply(lambda t: t.cpu())

    def type(self: T, dst_type: Union[dtype, str]) -> T:
        r"""Casts all parameters and buffers to :attr:`dst_type`.

        .. note::
            This method modifies the module in-place.

        Args:
            dst_type (type or string): the desired type

        Returns:
            Module: self
        """
        return self._apply(lambda t: t.type(dst_type))

    def float(self: T) -> T:
        r"""Casts all floating point parameters and buffers to ``float`` datatype.
        将所有浮点参数和缓冲区强制转换为浮点数据类型。

        .. note::
            This method modifies the module in-place.
            此方法将就地修改模块。

        Returns:
            Module: self
        """
        return self._apply(lambda t: t.float() if t.is_floating_point() else t)

    def double(self: T) -> T:
        r"""Casts all floating point parameters and buffers to ``double`` datatype.

        .. note::
            This method modifies the module in-place.

        Returns:
            Module: self
        """
        return self._apply(lambda t: t.double() if t.is_floating_point() else t)

    def half(self: T) -> T:
        r"""Casts all floating point parameters and buffers to ``half`` datatype.

        .. note::
            This method modifies the module in-place.

        Returns:
            Module: self
        """
        return self._apply(lambda t: t.half() if t.is_floating_point() else t)

    def bfloat16(self: T) -> T:
        r"""Casts all floating point parameters and buffers to ``bfloat16`` datatype.

        .. note::
            This method modifies the module in-place.

        Returns:
            Module: self
        """
        return self._apply(lambda t: t.bfloat16() if t.is_floating_point() else t)

    def to_empty(self: T, *, device: Union[str, device]) -> T:
        r"""Moves the parameters and buffers to the specified device without copying storage.

        Args:
            device (:class:`torch.device`): The desired device of the parameters
                and buffers in this module.

        Returns:
            Module: self
        """
        return self._apply(lambda t: torch.empty_like(t, device=device))

    @overload
    def to(self: T, device: Optional[Union[int, device]] = ..., dtype: Optional[Union[dtype, str]] = ...,
           non_blocking: bool = ...) -> T:
        ...

    @overload
    def to(self: T, dtype: Union[dtype, str], non_blocking: bool = ...) -> T:
        ...

    @overload
    def to(self: T, tensor: Tensor, non_blocking: bool = ...) -> T:
        ...

    def to(self, *args, **kwargs):
        r"""Moves and/or casts the parameters and buffers.
            移动和/或强制转换参数和缓冲区。

        This can be called as

        .. function:: to(device=None, dtype=None, non_blocking=False)
           :noindex:

        .. function:: to(dtype, non_blocking=False)
           :noindex:

        .. function:: to(tensor, non_blocking=False)
           :noindex:

        .. function:: to(memory_format=torch.channels_last)
           :noindex:

        Its signature is similar to :meth:`torch.Tensor.to`, but only accepts
        floating point or complex :attr:`dtype`\ s. In addition, this method will
        only cast the floating point or complex parameters and buffers to :attr:`dtype`
        (if given).
        它的签名类似于torch.Tensor.to()，但只接受浮点数或复杂的dtype’s。
        此外，该方法只将浮点数或复杂参数和缓冲区强制转换为:attr: ’ dtype(如果给定)。
        The integral parameters and buffers will be moved
        :attr:`device`, if that is given, but with dtypes unchanged. When
        :attr:`non_blocking` is set, it tries to convert/move asynchronously
        with respect to the host if possible, e.g., moving CPU Tensors with
        pinned memory to CUDA devices.
        如果给定，整型参数和缓冲区将被移到device，但dtype不变。当设置non_blocking时，
        如果可能的话，它会尝试相对于主机进行异步转换/移动，例如，将带有固定内存的CPU张量移动到CUDA设备。

        See below for examples.

        .. note::
            This method modifies the module in-place.
            此方法将就地修改模块。

        Args:
            device (:class:`torch.device`): the desired device of the parameters
                and buffers in this module
                在这个模块中参数和缓冲器的期望设备
            dtype (:class:`torch.dtype`): the desired floating point or complex dtype of
                the parameters and buffers in this module
                这个模块中参数和缓冲区的浮点型或复杂的Dtype
            tensor (torch.Tensor): Tensor whose dtype and device are the desired
                dtype and device for all parameters and buffers in this module
                张量的dtype和设备是这个模块中所有参数和缓冲区所需的dtype和设备
            memory_format (:class:`torch.memory_format`): the desired memory
                format for 4D parameters and buffers in this module (keyword
                only argument)
                该模块中4D参数和缓冲区所需的内存格式(仅关键字参数)

        Returns:
            Module: self

        Examples::

            >>> linear = nn.Linear(2, 2)
            >>> linear.weight
            Parameter containing:
            tensor([[ 0.1913, -0.3420],
                    [-0.5113, -0.2325]])
            >>> linear.to(torch.double)
            Linear(in_features=2, out_features=2, bias=True)
            >>> linear.weight
            Parameter containing:
            tensor([[ 0.1913, -0.3420],
                    [-0.5113, -0.2325]], dtype=torch.float64)
            >>> gpu1 = torch.device("cuda:1")
            >>> linear.to(gpu1, dtype=torch.half, non_blocking=True)
            Linear(in_features=2, out_features=2, bias=True)
            >>> linear.weight
            Parameter containing:
            tensor([[ 0.1914, -0.3420],
                    [-0.5112, -0.2324]], dtype=torch.float16, device='cuda:1')
            >>> cpu = torch.device("cpu")
            >>> linear.to(cpu)
            Linear(in_features=2, out_features=2, bias=True)
            >>> linear.weight
            Parameter containing:
            tensor([[ 0.1914, -0.3420],
                    [-0.5112, -0.2324]], dtype=torch.float16)

            >>> linear = nn.Linear(2, 2, bias=None).to(torch.cdouble)
            >>> linear.weight
            Parameter containing:
            tensor([[ 0.3741+0.j,  0.2382+0.j],
                    [ 0.5593+0.j, -0.4443+0.j]], dtype=torch.complex128)
            >>> linear(torch.ones(3, 2, dtype=torch.cdouble))
            tensor([[0.6122+0.j, 0.1150+0.j],
                    [0.6122+0.j, 0.1150+0.j],
                    [0.6122+0.j, 0.1150+0.j]], dtype=torch.complex128)

        """

        device, dtype, non_blocking, convert_to_format = torch._C._nn._parse_to(*args, **kwargs)

        if dtype is not None:
            if not (dtype.is_floating_point or dtype.is_complex):
                raise TypeError('nn.Module.to only accepts floating point or complex '
                                'dtypes, but got desired dtype={}'.format(dtype))
            if dtype.is_complex:
                warnings.warn(
                    "Complex modules are a new feature under active development whose design may change, "
                    "and some modules might not work as expected when using complex tensors as parameters or buffers. "
                    "Please file an issue at https://github.com/pytorch/pytorch/issues/new?template=bug-report.yml "
                    "if a complex module does not work as expected.")

        def convert(t):
            if convert_to_format is not None and t.dim() in (4, 5):
                return t.to(device, dtype if t.is_floating_point() or t.is_complex() else None,
                            non_blocking, memory_format=convert_to_format)
            return t.to(device, dtype if t.is_floating_point() or t.is_complex() else None, non_blocking)

        return self._apply(convert)

    def register_backward_hook(
            self, hook: Callable[['Module', _grad_t, _grad_t], Union[None, Tensor]]
    ) -> RemovableHandle:
        r"""Registers a backward hook on the module.

        This function is deprecated in favor of :meth:`~torch.nn.Module.register_full_backward_hook` and
        the behavior of this function will change in future versions.

        Returns:
            :class:`torch.utils.hooks.RemovableHandle`:
                a handle that can be used to remove the added hook by calling
                ``handle.remove()``

        """
        if self._is_full_backward_hook is True:
            raise RuntimeError("Cannot use both regular backward hooks and full backward hooks on a "
                               "single Module. Please use only one of them.")

        self._is_full_backward_hook = False

        handle = hooks.RemovableHandle(self._backward_hooks)
        self._backward_hooks[handle.id] = hook
        return handle

    def register_full_backward_hook(
            self, hook: Callable[['Module', _grad_t, _grad_t], Union[None, Tensor]]
    ) -> RemovableHandle:
        r"""Registers a backward hook on the module.

        The hook will be called every time the gradients with respect to module
        inputs are computed. The hook should have the following signature::

            hook(module, grad_input, grad_output) -> tuple(Tensor) or None

        The :attr:`grad_input` and :attr:`grad_output` are tuples that contain the gradients
        with respect to the inputs and outputs respectively. The hook should
        not modify its arguments, but it can optionally return a new gradient with
        respect to the input that will be used in place of :attr:`grad_input` in
        subsequent computations. :attr:`grad_input` will only correspond to the inputs given
        as positional arguments and all kwarg arguments are ignored. Entries
        in :attr:`grad_input` and :attr:`grad_output` will be ``None`` for all non-Tensor
        arguments.

        For technical reasons, when this hook is applied to a Module, its forward function will
        receive a view of each Tensor passed to the Module. Similarly the caller will receive a view
        of each Tensor returned by the Module's forward function.

        .. warning ::
            Modifying inputs or outputs inplace is not allowed when using backward hooks and
            will raise an error.

        Returns:
            :class:`torch.utils.hooks.RemovableHandle`:
                a handle that can be used to remove the added hook by calling
                ``handle.remove()``

        """
        if self._is_full_backward_hook is False:
            raise RuntimeError("Cannot use both regular backward hooks and full backward hooks on a "
                               "single Module. Please use only one of them.")

        self._is_full_backward_hook = True

        handle = hooks.RemovableHandle(self._backward_hooks)
        self._backward_hooks[handle.id] = hook
        return handle

    def _get_backward_hooks(self):
        r"""Returns the backward hooks for use in the call function.
        It returns two lists, one with the full backward hooks and one with the non-full
        backward hooks.
        """
        full_backward_hooks: List[Callable] = []
        if (_global_is_full_backward_hook is True):
            full_backward_hooks += _global_backward_hooks.values()
        if (self._is_full_backward_hook is True):
            full_backward_hooks += self._backward_hooks.values()

        non_full_backward_hooks: List[Callable] = []
        if (_global_is_full_backward_hook is False):
            non_full_backward_hooks += _global_backward_hooks.values()
        if (self._is_full_backward_hook is False):
            non_full_backward_hooks += self._backward_hooks.values()

        return full_backward_hooks, non_full_backward_hooks

    def _maybe_warn_non_full_backward_hook(self, inputs, result, grad_fn):
        if not isinstance(result, torch.Tensor):
            if not (isinstance(result, tuple) and all([isinstance(r, torch.Tensor) for r in result])):
                warnings.warn("Using non-full backward hooks on a Module that does not return a "
                              "single Tensor or a tuple of Tensors is deprecated and will be removed "
                              "in future versions. This hook will be missing some of the grad_output. "
                              "Please use register_full_backward_hook to get the documented behavior.")
                return
        else:
            result = (result,)

        if not isinstance(inputs, torch.Tensor):
            if not (isinstance(inputs, tuple) and all([isinstance(i, torch.Tensor) for i in inputs])):
                warnings.warn("Using non-full backward hooks on a Module that does not take as input a "
                              "single Tensor or a tuple of Tensors is deprecated and will be removed "
                              "in future versions. This hook will be missing some of the grad_input. "
                              "Please use register_full_backward_hook to get the documented behavior.")
                return
        else:
            inputs = (inputs,)

        # At this point we are sure that inputs and result are tuple of Tensors
        out_grad_fn = {r.grad_fn for r in result if r.grad_fn is not None}
        if len(out_grad_fn) == 0 or (len(out_grad_fn) == 1 and grad_fn not in out_grad_fn):
            warnings.warn("Using a non-full backward hook when outputs are nested in python data structure "
                          "is deprecated and will be removed in future versions. This hook will be missing "
                          "some grad_output.")
        elif len(out_grad_fn) > 1:
            warnings.warn("Using a non-full backward hook when outputs are generated by different autograd Nodes "
                          "is deprecated and will be removed in future versions. This hook will be missing "
                          "some grad_output. Please use register_full_backward_hook to get the documented behavior.")
        else:
            # At this point the grad_ouput part of the hook will most likely be correct
            inputs_grad_fn = {i.grad_fn for i in inputs if i.grad_fn is not None}

            next_functions = {n[0] for n in grad_fn.next_functions}

            if inputs_grad_fn != next_functions:
                warnings.warn("Using a non-full backward hook when the forward contains multiple autograd Nodes "
                              "is deprecated and will be removed in future versions. This hook will be missing "
                              "some grad_input. Please use register_full_backward_hook to get the documented "
                              "behavior.")

    def register_forward_pre_hook(self, hook: Callable[..., None]) -> RemovableHandle:
        r"""Registers a forward pre-hook on the module.

        The hook will be called every time before :func:`forward` is invoked.
        It should have the following signature::

            hook(module, input) -> None or modified input

        The input contains only the positional arguments given to the module.
        Keyword arguments won't be passed to the hooks and only to the ``forward``.
        The hook can modify the input. User can either return a tuple or a
        single modified value in the hook. We will wrap the value into a tuple
        if a single value is returned(unless that value is already a tuple).

        Returns:
            :class:`torch.utils.hooks.RemovableHandle`:
                a handle that can be used to remove the added hook by calling
                ``handle.remove()``
        """
        handle = hooks.RemovableHandle(self._forward_pre_hooks)
        self._forward_pre_hooks[handle.id] = hook
        return handle

    def register_forward_hook(self, hook: Callable[..., None]) -> RemovableHandle:
        r"""Registers a forward hook on the module.

        The hook will be called every time after :func:`forward` has computed an output.
        It should have the following signature::

            hook(module, input, output) -> None or modified output

        The input contains only the positional arguments given to the module.
        Keyword arguments won't be passed to the hooks and only to the ``forward``.
        The hook can modify the output. It can modify the input inplace but
        it will not have effect on forward since this is called after
        :func:`forward` is called.

        Returns:
            :class:`torch.utils.hooks.RemovableHandle`:
                a handle that can be used to remove the added hook by calling
                ``handle.remove()``
        """
        handle = hooks.RemovableHandle(self._forward_hooks)
        self._forward_hooks[handle.id] = hook
        return handle

    def _slow_forward(self, *input, **kwargs):
        tracing_state = torch._C._get_tracing_state()
        if not tracing_state or isinstance(self.forward, torch._C.ScriptMethod):
            return self.forward(*input, **kwargs)
        recording_scopes = torch.jit._trace._trace_module_map is not None
        if recording_scopes:
            # type ignore was added because at this point one knows that
            # torch.jit._trace._trace_module_map is not Optional and has type Dict[Any, Any]
            name = torch.jit._trace._trace_module_map[
                self] if self in torch.jit._trace._trace_module_map else None  # type: ignore[index, operator] # noqa: B950
            if name:
                tracing_state.push_scope(name)
            else:
                recording_scopes = False
        try:
            result = self.forward(*input, **kwargs)
        finally:
            if recording_scopes:
                tracing_state.pop_scope()
        return result

    def _call_impl(self, *input, **kwargs):
        forward_call = (self._slow_forward if torch._C._get_tracing_state() else self.forward)
        # If we don't have any hooks, we want to skip the rest of the logic in
        # this function, and just call forward.
        if not (self._backward_hooks or self._forward_hooks or self._forward_pre_hooks or _global_backward_hooks
                or _global_forward_hooks or _global_forward_pre_hooks):
            return forward_call(*input, **kwargs)
        # Do not call functions when jit is used
        full_backward_hooks, non_full_backward_hooks = [], []
        if self._backward_hooks or _global_backward_hooks:
            full_backward_hooks, non_full_backward_hooks = self._get_backward_hooks()
        if _global_forward_pre_hooks or self._forward_pre_hooks:
            for hook in (*_global_forward_pre_hooks.values(), *self._forward_pre_hooks.values()):
                result = hook(self, input)
                if result is not None:
                    if not isinstance(result, tuple):
                        result = (result,)
                    input = result

        bw_hook = None
        if full_backward_hooks:
            bw_hook = hooks.BackwardHook(self, full_backward_hooks)
            input = bw_hook.setup_input_hook(input)

        result = forward_call(*input, **kwargs)
        if _global_forward_hooks or self._forward_hooks:
            for hook in (*_global_forward_hooks.values(), *self._forward_hooks.values()):
                hook_result = hook(self, input, result)
                if hook_result is not None:
                    result = hook_result

        if bw_hook:
            result = bw_hook.setup_output_hook(result)

        # Handle the non-full backward hooks
        if non_full_backward_hooks:
            var = result
            while not isinstance(var, torch.Tensor):
                if isinstance(var, dict):
                    var = next((v for v in var.values() if isinstance(v, torch.Tensor)))
                else:
                    var = var[0]
            grad_fn = var.grad_fn
            if grad_fn is not None:
                for hook in non_full_backward_hooks:
                    wrapper = functools.partial(hook, self)
                    functools.update_wrapper(wrapper, hook)
                    grad_fn.register_hook(wrapper)
                self._maybe_warn_non_full_backward_hook(input, result, grad_fn)

        return result

    __call__: Callable[..., Any] = _call_impl

    def __setstate__(self, state):
        self.__dict__.update(state)
        # Support loading old checkpoints that don't have the following attrs:
        if '_forward_pre_hooks' not in self.__dict__:
            self._forward_pre_hooks = OrderedDict()
        if '_state_dict_hooks' not in self.__dict__:
            self._state_dict_hooks = OrderedDict()
        if '_load_state_dict_pre_hooks' not in self.__dict__:
            self._load_state_dict_pre_hooks = OrderedDict()
        if '_load_state_dict_post_hooks' not in self.__dict__:
            self._load_state_dict_post_hooks = OrderedDict()
        if '_non_persistent_buffers_set' not in self.__dict__:
            self._non_persistent_buffers_set = set()
        if '_is_full_backward_hook' not in self.__dict__:
            self._is_full_backward_hook = None

    def __getattr__(self, name: str) -> Union[Tensor, 'Module']:
        """
        为了确保 getattr 能访问到所有的属性，nn.Module 也重载了 __getattr__ 函数，
        以访问 self._parameters，self._buffers，self._modules 中的属性。
        根据 Python 对实例属性的查找规则，当我们调用 module.attribute 的时候，
        Python 会首先查找 module 的 类及其基类的 __dict__，然后查找这个 object 的 __dict__，最后查找 __getattr__ 函数。
        因此，虽然 nn.Module 的 __getattr__ 只查找了 self._parameters，self._buffers，self._modules 三个成员变量，
        但是 getattr(module, 'attribute') 覆盖的范围和 __dir__ 暴露的范围是一致的。
        """
        if '_parameters' in self.__dict__:  # 判断_parameters是否在模块的属性字典中
            _parameters = self.__dict__['_parameters']  # 获取属性'_parameters'的OrderedDict字典对象
            if name in _parameters:  # 判断name是否在OrderedDict字典对象中
                return _parameters[name]
        if '_buffers' in self.__dict__:
            _buffers = self.__dict__['_buffers']
            if name in _buffers:
                return _buffers[name]
        if '_modules' in self.__dict__:
            modules = self.__dict__['_modules']
            if name in modules:
                return modules[name]
        raise AttributeError("'{}' object has no attribute '{}'".format(
            type(self).__name__, name))

    def __setattr__(self, name: str, value: Union[Tensor, 'Module']) -> None:
        """
        在日常的代码开发过程中，更常见的用法是直接通过 self.xxx = xxx 的方式来增加或修改子神经网络模块、p
        arameters、buffers 以及其他一般的 attribute。这种方式本质上会调用 nn.Module 重载的函数 __setattr__

        从源码中有如下观察：
        1、函数检查了继承 nn.Module 的自定义模块是否有正确地初始化父类 nn.Module，这也说明了 super().__init__() 的必要性
        2、在增加 self._parameters，self._modules 的时候，会预先调用 remove_from 函数 从其余私有属性中删除对应的 name，
            这说明 self.dict，self._buffers，self._parameters，self._modules 中的属性应该是互斥的
        3、如果要给模块增加 buffer，self.register_buffer 是唯一的方式，__setattr__ 只能将 self._buffers 中已有的 buffer
            重新赋值为 None 或者 tensor 。这是因为 buffer 的初始化类型就是 torch.Tensor 或者 None，
            而不像 parameters 和 module 分别是 nn.Parameter 和 nn.Module 类型
        4、除了其他普通的 attribute，最终 parameters 还是会在 __setattr__ 中通过 register_parameter 来增加，
            但是子神经网络模块和 buffer 是直接修改的 self._modules 和 self._buffers
        5、由第三点和前文所述的 _apply 实现可以得出 self.xxx = torch.Tensor() 是一种不被推荐的行为，
            因为这样新增的 attribute 既不属于 self._parameters，也不属于 self._buffers，
            而会被视为普通的 attribute ，在将模块进行状态转换的时候，self.xxx 会被遗漏进而导致 device 或者 type 不一样的 bug

        """
        def remove_from(*dicts_or_sets):
            for d in dicts_or_sets:
                if name in d:
                    if isinstance(d, dict): # 在字典中
                        del d[name]
                    else:# 在集合中
                        d.discard(name)

        params = self.__dict__.get('_parameters') # 获取'_parameters'的OrderedDict()字典对象
        if isinstance(value, Parameter): # 判断value是否是Parameter对象
            if params is None:
                raise AttributeError(
                    "cannot assign parameters before Module.__init__() call")
            # params为None，调用Module.__init__()之前，需要先将键为name的参数删除，保证调用调用register_parameter方法不会抛出异常
            # 因为if param is None:
            #       self._parameters[name] = None
            # 否则抛出异常："cannot assign '{}' object to parameter '{}' ""(torch.nn.Parameter or None required)"
            remove_from(self.__dict__, self._buffers, self._modules, self._non_persistent_buffers_set)
            # 调用register_parameter，将名为name，参数值为value的参数添加到当前模块
            self.register_parameter(name, value)
        elif params is not None and name in params:
            if value is not None:
                raise TypeError("cannot assign '{}' as parameter '{}' "
                                "(torch.nn.Parameter or None expected)"
                                .format(torch.typename(value), name))

            # 调用register_parameter，将名为name，参数值为value的参数添加到当前模块
            self.register_parameter(name, value)
        else:
            modules = self.__dict__.get('_modules')# 获取'_modules'的OrderedDict()字典对象
            if isinstance(value, Module):
                if modules is None:
                    raise AttributeError(
                        "cannot assign module before Module.__init__() call")
                remove_from(self.__dict__, self._parameters, self._buffers, self._non_persistent_buffers_set)
                modules[name] = value
            elif modules is not None and name in modules:
                if value is not None:
                    raise TypeError("cannot assign '{}' as child module '{}' "
                                    "(torch.nn.Module or None expected)"
                                    .format(torch.typename(value), name))
                modules[name] = value
            else:
                buffers = self.__dict__.get('_buffers')# 获取'_buffers'的OrderedDict()字典对象
                if buffers is not None and name in buffers:
                    if value is not None and not isinstance(value, torch.Tensor):
                        raise TypeError("cannot assign '{}' as buffer '{}' "
                                        "(torch.Tensor or None expected)"
                                        .format(torch.typename(value), name))
                    buffers[name] = value
                else:
                    object.__setattr__(self, name, value)

    def __delattr__(self, name):
        """
        __delattr__ 会挨个检查 self._parameters、self._buffers、self._modules 和普通的 attribute 并将 name 从中删除。
        """
        if name in self._parameters:
            del self._parameters[name]
        elif name in self._buffers:
            del self._buffers[name]
            self._non_persistent_buffers_set.discard(name)
        elif name in self._modules:
            del self._modules[name]
        else:
            object.__delattr__(self, name)

    def _register_state_dict_hook(self, hook):
        r"""These hooks will be called with arguments: `self`, `state_dict`,
        `prefix`, `local_metadata`, after the `state_dict` of `self` is set.
        Note that only parameters and buffers of `self` or its children are
        guaranteed to exist in `state_dict`. The hooks may modify `state_dict`
        inplace or return a new one.
        """
        handle = hooks.RemovableHandle(self._state_dict_hooks)
        self._state_dict_hooks[handle.id] = hook
        return handle

    def _save_to_state_dict(self, destination, prefix, keep_vars):
        r"""Saves module state to `destination` dictionary, containing a state
        of the module, but not its descendants. This is called on every
        submodule in :meth:`~torch.nn.Module.state_dict`.

        In rare cases, subclasses can achieve class-specific behavior by
        overriding this method with custom logic.

        Args:
            destination (dict): a dict where state will be stored
            prefix (str): the prefix for parameters and buffers used in this
                module
        """
        for name, param in self._parameters.items():
            if param is not None:
                destination[prefix + name] = param if keep_vars else param.detach()
        for name, buf in self._buffers.items():
            if buf is not None and name not in self._non_persistent_buffers_set:
                destination[prefix + name] = buf if keep_vars else buf.detach()
        extra_state_key = prefix + _EXTRA_STATE_KEY_SUFFIX
        if getattr(self.__class__, "get_extra_state", Module.get_extra_state) is not Module.get_extra_state:
            destination[extra_state_key] = self.get_extra_state()

    # The user can pass an optional arbitrary mappable object to `state_dict`, in which case `state_dict` returns
    # back that same object. But if they pass nothing, an `OrederedDict` is created and returned.
    T_destination = TypeVar('T_destination', bound=Dict[str, Any])

    @overload
    def state_dict(self, *, destination: T_destination, prefix: str = ..., keep_vars: bool = ...) -> T_destination:
        ...

    @overload
    def state_dict(self, *, prefix: str = ..., keep_vars: bool = ...) -> Dict[str, Any]:
        ...

    # TODO: Change `*args` to `*` and remove the copprespinding warning in docs when BC allows.
    # Also remove the logic for arg parsing together.
    def state_dict(self, *args, destination=None, prefix='', keep_vars=False):
        r"""Returns a dictionary containing a whole state of the module.
            返回包含整个模块状态的字典。

        Both parameters and persistent buffers (e.g. running averages) are
        included. Keys are corresponding parameter and buffer names.
        Parameters and buffers set to ``None`` are not included.
        **参数和持久缓冲区(**例如运行平均值)都包括在内。键是相应的参数和缓冲区名称。

        .. warning::
            Currently ``state_dict()`` also accepts positional arguments for
            ``destination``, ``prefix`` and ``keep_vars`` in order. However,
            this is being deprecated and keyword arguments will be enforced in
            future releases.

        .. warning::
            Please avoid the use of argument ``destination`` as it is not
            designed for end-users.

        Args:
            destination (dict, optional): If provided, the state of module will
                be updated into the dict and the same object is returned.
                Otherwise, an ``OrderedDict`` will be created and returned.
                Default: ``None``.
            prefix (str, optional): a prefix added to parameter and buffer
                names to compose the keys in state_dict. Default: ``''``.
            keep_vars (bool, optional): by default the :class:`~torch.Tensor` s
                returned in the state dict are detached from autograd. If it's
                set to ``True``, detaching will not be performed.
                Default: ``False``.

        Returns:
            dict:
                a dictionary containing a whole state of the module
                包含模块的整个状态的字典

        Example::

            >>> module.state_dict().keys()
            ['bias', 'weight']

        """

        # TODO: Remove `args` and the parsing logic when BC allows.
        if len(args) > 0:
            if destination is None:
                destination = args[0]
            if len(args) > 1 and prefix == '':
                prefix = args[1]
            if len(args) > 2 and keep_vars is False:
                keep_vars = args[2]
            # DeprecationWarning is ignored by default
            warnings.warn(
                "Positional args are being deprecated, use kwargs instead. Refer to "
                "https://pytorch.org/docs/master/generated/torch.nn.Module.html#torch.nn.Module.state_dict"
                " for details.")

        if destination is None:
            destination = OrderedDict()
            destination._metadata = OrderedDict()

        local_metadata = dict(version=self._version)
        if hasattr(destination, "_metadata"):
            destination._metadata[prefix[:-1]] = local_metadata

        self._save_to_state_dict(destination, prefix, keep_vars)
        for name, module in self._modules.items():
            if module is not None:
                module.state_dict(destination=destination, prefix=prefix + name + '.', keep_vars=keep_vars)
        for hook in self._state_dict_hooks.values():
            hook_result = hook(self, destination, prefix, local_metadata)
            if hook_result is not None:
                destination = hook_result
        return destination

    def _register_load_state_dict_pre_hook(self, hook, with_module=False):
        r"""These hooks will be called with arguments: `state_dict`, `prefix`,
        `local_metadata`, `strict`, `missing_keys`, `unexpected_keys`,
        `error_msgs`, before loading `state_dict` into `self`. These arguments
        are exactly the same as those of `_load_from_state_dict`.

        If ``with_module`` is ``True``, then the first argument to the hook is
        an instance of the module.

        Arguments:
            hook (Callable): Callable hook that will be invoked before
                loading the state dict.
            with_module (bool, optional): Whether or not to pass the module
                instance to the hook as the first parameter.
        """
        handle = hooks.RemovableHandle(self._load_state_dict_pre_hooks)
        if with_module:
            hook = functools.partial(hook, self)
        self._load_state_dict_pre_hooks[handle.id] = hook
        return handle

    def register_load_state_dict_post_hook(self, hook):
        r"""Registers a post hook to be run after module's ``load_state_dict``
        is called.

        It should have the following signature::
            hook(module, incompatible_keys) -> None

        The ``module`` argument is the current module that this hook is registered
        on, and the ``incompatible_keys`` argument is a ``NamedTuple`` consisting
        of attributes ``missing_keys`` and ``unexpected_keys``. ``missing_keys``
        is a ``list`` of ``str`` containing the missing keys and
        ``unexpected_keys`` is a ``list`` of ``str`` containing the unexpected keys.

        The given incompatible_keys can be modified inplace if needed.

        Note that the checks performed when calling :func:`load_state_dict` with
        ``strict=True`` are affected by modifications the hook makes to
        ``missing_keys`` or ``unexpected_keys``, as expected. Additions to either
        set of keys will result in an error being thrown when ``strict=True``, and
        clearning out both missing and unexpected keys will avoid an error.

        Returns:
            :class:`torch.utils.hooks.RemovableHandle`:
                a handle that can be used to remove the added hook by calling
                ``handle.remove()``
        """
        handle = hooks.RemovableHandle(self._load_state_dict_post_hooks)
        self._load_state_dict_post_hooks[handle.id] = hook
        return handle

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        r"""Copies parameters and buffers from :attr:`state_dict` into only
        this module, but not its descendants. This is called on every submodule
        in :meth:`~torch.nn.Module.load_state_dict`. Metadata saved for this
        module in input :attr:`state_dict` is provided as :attr:`local_metadata`.
        For state dicts without metadata, :attr:`local_metadata` is empty.
        Subclasses can achieve class-specific backward compatible loading using
        the version number at `local_metadata.get("version", None)`.

        .. note::
            :attr:`state_dict` is not the same object as the input
            :attr:`state_dict` to :meth:`~torch.nn.Module.load_state_dict`. So
            it can be modified.

        Args:
            state_dict (dict): a dict containing parameters and
                persistent buffers.
            prefix (str): the prefix for parameters and buffers used in this
                module
            local_metadata (dict): a dict containing the metadata for this module.
                See
            strict (bool): whether to strictly enforce that the keys in
                :attr:`state_dict` with :attr:`prefix` match the names of
                parameters and buffers in this module
            missing_keys (list of str): if ``strict=True``, add missing keys to
                this list
            unexpected_keys (list of str): if ``strict=True``, add unexpected
                keys to this list
            error_msgs (list of str): error messages should be added to this
                list, and will be reported together in
                :meth:`~torch.nn.Module.load_state_dict`
        """
        for hook in self._load_state_dict_pre_hooks.values():
            hook(state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs)

        persistent_buffers = {k: v for k, v in self._buffers.items() if k not in self._non_persistent_buffers_set}
        local_name_params = itertools.chain(self._parameters.items(), persistent_buffers.items())
        local_state = {k: v for k, v in local_name_params if v is not None}

        for name, param in local_state.items():
            key = prefix + name
            if key in state_dict:
                input_param = state_dict[key]
                if not torch.overrides.is_tensor_like(input_param):
                    error_msgs.append('While copying the parameter named "{}", '
                                      'expected torch.Tensor or Tensor-like object from checkpoint but '
                                      'received {}'
                                      .format(key, type(input_param)))
                    continue

                # This is used to avoid copying uninitialized parameters into
                # non-lazy modules, since they dont have the hook to do the checks
                # in such case, it will error when accessing the .shape attribute.
                is_param_lazy = torch.nn.parameter.is_lazy(param)
                # Backward compatibility: loading 1-dim tensor from 0.3.* to version 0.4+
                if not is_param_lazy and len(param.shape) == 0 and len(input_param.shape) == 1:
                    input_param = input_param[0]

                if not is_param_lazy and input_param.shape != param.shape:
                    # local shape should match the one in checkpoint
                    error_msgs.append('size mismatch for {}: copying a param with shape {} from checkpoint, '
                                      'the shape in current model is {}.'
                                      .format(key, input_param.shape, param.shape))
                    continue
                try:
                    with torch.no_grad():
                        param.copy_(input_param)
                except Exception as ex:
                    error_msgs.append('While copying the parameter named "{}", '
                                      'whose dimensions in the model are {} and '
                                      'whose dimensions in the checkpoint are {}, '
                                      'an exception occurred : {}.'
                                      .format(key, param.size(), input_param.size(), ex.args))
            elif strict:
                missing_keys.append(key)

        extra_state_key = prefix + _EXTRA_STATE_KEY_SUFFIX
        if getattr(self.__class__, "set_extra_state", Module.set_extra_state) is not Module.set_extra_state:
            if extra_state_key in state_dict:
                self.set_extra_state(state_dict[extra_state_key])
            elif strict:
                missing_keys.append(extra_state_key)
        elif strict and (extra_state_key in state_dict):
            unexpected_keys.append(extra_state_key)

        if strict:
            for key in state_dict.keys():
                if key.startswith(prefix) and key != extra_state_key:
                    input_name = key[len(prefix):]
                    input_name = input_name.split('.', 1)[0]  # get the name of param/buffer/child
                    if input_name not in self._modules and input_name not in local_state:
                        unexpected_keys.append(key)

    def load_state_dict(self, state_dict: Mapping[str, Any],
                        strict: bool = True):
        r"""Copies parameters and buffers from :attr:`state_dict` into
        this module and its descendants. If :attr:`strict` is ``True``, then
        the keys of :attr:`state_dict` must exactly match the keys returned
        by this module's :meth:`~torch.nn.Module.state_dict` function.
        将state_dict中的参数和缓冲区复制到该模块及其子模块中。
        如果strict为True，则state_dict的键必须与该模块的*state_dict()*函数返回的键完全匹配。

        Args:
            state_dict (dict): a dict containing parameters and
                persistent buffers.
             一个包含参数和持久缓冲区的字典。

            strict (bool, optional): whether to strictly enforce that the keys
                in :attr:`state_dict` match the keys returned by this module's
                :meth:`~torch.nn.Module.state_dict` function. Default: ``True``
            是否严格强制state_dict中的键与该模块的*state_dict()*函数返回的键相匹配。默认值:Ture

        Returns:
            ``NamedTuple`` with ``missing_keys`` and ``unexpected_keys`` fields:
                * **missing_keys** is a list of str containing the missing keys
                * **unexpected_keys** is a list of str containing the unexpected keys
                Missing_keys是一个包含丢失键的字符串列表
                unexpected keys是一个包含意外键的字符串列表
                Return type：NamedTuple with missing_keys and unexpected_keys fields

        Note:
            If a parameter or buffer is registered as ``None`` and its corresponding key
            exists in :attr:`state_dict`, :meth:`load_state_dict` will raise a
            ``RuntimeError``.
            如果参数或缓冲区注册为“ None”及其对应的键存在于: attr: ‘ state _ dict’，
            方法‘ load _ state _ dict’将引发一个``RuntimeError``
        """
        if not isinstance(state_dict, Mapping):
            raise TypeError("Expected state_dict to be dict-like, got {}.".format(type(state_dict)))

        missing_keys: List[str] = []
        unexpected_keys: List[str] = []
        error_msgs: List[str] = []

        # copy state_dict so _load_from_state_dict can modify it
        metadata = getattr(state_dict, '_metadata', None)
        state_dict = OrderedDict(state_dict) # 实例化OrderedDict，自动调用copy()方法，浅拷贝
        if metadata is not None:
            # mypy isn't aware that "_metadata" exists in state_dict
            state_dict._metadata = metadata  # type: ignore[attr-defined]

        def load(module, prefix=''):
            local_metadata = {} if metadata is None else metadata.get(prefix[:-1], {})
            module._load_from_state_dict(
                state_dict, prefix, local_metadata, True, missing_keys, unexpected_keys, error_msgs)
            for name, child in module._modules.items():
                if child is not None:
                    load(child, prefix + name + '.')

            # Note that the hook can modify missing_keys and unexpected_keys.
            incompatible_keys = _IncompatibleKeys(missing_keys, unexpected_keys)
            for hook in module._load_state_dict_post_hooks.values():
                out = hook(module, incompatible_keys)
                assert out is None, (
                    "Hooks registered with ``register_load_state_dict_post_hook`` are not"
                    "expected to return new values, if incompatible_keys need to be modified,"
                    "it should be done inplace."
                )

        load(self)
        del load

        if strict:
            if len(unexpected_keys) > 0:
                error_msgs.insert(
                    0, 'Unexpected key(s) in state_dict: {}. '.format(
                        ', '.join('"{}"'.format(k) for k in unexpected_keys)))
            if len(missing_keys) > 0:
                error_msgs.insert(
                    0, 'Missing key(s) in state_dict: {}. '.format(
                        ', '.join('"{}"'.format(k) for k in missing_keys)))

        if len(error_msgs) > 0:
            raise RuntimeError('Error(s) in loading state_dict for {}:\n\t{}'.format(
                self.__class__.__name__, "\n\t".join(error_msgs)))
        return _IncompatibleKeys(missing_keys, unexpected_keys)

    def _named_members(self, get_members_fn, prefix='', recurse=True):
        r"""Helper method for yielding various names + members of modules.
        产生各种名称的成员模块的辅助方法，该方法的目的是为了确定是获取当前模块还是所有模块的属性的名称和值
        """
        memo = set()
        # 三元表达式，如果为recurse=True，返回所有模块，如果为False仅返回当前模块
        modules = self.named_modules(prefix=prefix) if recurse else [(prefix, self)]
        for module_prefix, module in modules:
            # 使用lambda方法获取模块某个属性的名称和值的字典，并赋值给members
            members = get_members_fn(module)
            for k, v in members:
                if v is None or v in memo:
                    continue
                memo.add(v)
                name = module_prefix + ('.' if module_prefix else '') + k
                yield name, v # 返回属性的名称和值
    """
    常见的属性访问方法

    nn.Module 中的常用函数包括下面 8 个，他们都会返回一个迭代器用于访问模块中的 buffer，parameter，子模块等。他们的功能与区别如下
        1、parameters：调用 self.named_parameters 并返回模型参数，被应用于 self.requires_grad_ 和 self.zero_grad 函数中
        2、named_parameters：返回 self._parameters 中的 name 和 parameter 元组，如果 recurse=True 还会返回子模块中的模型参数
        3、buffers：调用 self.named_buffers 并返回模型参数
        4、named_buffers：返回 self._buffers 中的 name 和 buffer 元组，如果 recurse=True 还会返回子模块中的模型 buffer
        5、children：调用 self.named_children，只返回 self._modules 中的模块，被应用于 self.train 函数中
        6、named_children：只返回 self._modules 中的 name 和 module 元组
        7、modules：调用 self.named_modules 并返回各个 module 但不返回 name
        8、named_modules：返回 self._modules 下的 name 和 module 元组，并递归调用和返回 module.named_modules
    
    named_parameters 和 named_buffers 都是调用的 self._named_members 实现的，
    named_modules 和 named_children 虽然有自己的实现，但和 self._named_members 一样，
    都是通过 set 类型的 memo 来记录已经抛出的模块，如果 member 不在 memo 中，
    才会将 member 抛出并将 member 放入 memo 中，因此 named_parameters、named_buffers、
    named_modules 和named_children 都不会返回重复的 parameter、 buffer 或 module。

    """
    def parameters(self, recurse: bool = True) -> Iterator[Parameter]:
        r"""Returns an iterator over module parameters.
            返回模块参数上的迭代器。

        This is typically passed to an optimizer.
        这通常被传递给优化器。

        Args:
            recurse (bool): if True, then yields parameters of this module
                and all submodules. Otherwise, yields only parameters that
                are direct members of this module.
                如果为True，则生成该模块和所有子模块的参数。否则，只生成该模块的直接成员参数。

        Yields:
            Parameter: module parameter

        Example::

            >>> for param in model.parameters():
            >>>     print(type(param), param.size())
            <class 'torch.Tensor'> (20L,)
            <class 'torch.Tensor'> (20L, 1L, 5L, 5L)

        """
        for name, param in self.named_parameters(recurse=recurse):
            yield param

    def named_parameters(self, prefix: str = '', recurse: bool = True) -> Iterator[Tuple[str, Parameter]]:
        r"""Returns an iterator over module parameters, yielding both the
        name of the parameter as well as the parameter itself.
        返回模块参数迭代器，产生参数名称和参数值

        Args:
            prefix (str): prefix to prepend to all parameter names.
            recurse (bool): if True, then yields parameters of this module
                and all submodules. Otherwise, yields only parameters that
                are direct members of this module.
                如果为True，生成该模块和所有子模块的参数迭代器，如果为False，仅返回当前模块

        Yields:
            (string, Parameter): Tuple containing the name and parameter

        Example::

            >>> for name, param in self.named_parameters():
            >>>    if name in ['bias']:
            >>>        print(param.size())

        """
        gen = self._named_members(
            lambda module: module._parameters.items(),
            prefix=prefix, recurse=recurse)
        for elem in gen:
            yield elem

    def buffers(self, recurse: bool = True) -> Iterator[Tensor]:
        r"""Returns an iterator over module buffers.
        返回一个模块缓冲区迭代器

        Args:
            recurse (bool): if True, then yields buffers of this module
                and all submodules. Otherwise, yields only buffers that
                are direct members of this module.
            如果为True，生成该模块和所有子模块的buffers迭代器，如果为False，仅返回当前模块
        Yields:
            torch.Tensor: module buffer

        Example::

            >>> for buf in model.buffers():
            >>>     print(type(buf), buf.size())
            <class 'torch.Tensor'> (20L,)
            <class 'torch.Tensor'> (20L, 1L, 5L, 5L)

        """
        for _, buf in self.named_buffers(recurse=recurse):
            yield buf

    def named_buffers(self, prefix: str = '', recurse: bool = True) -> Iterator[Tuple[str, Tensor]]:
        r"""Returns an iterator over module buffers, yielding both the
        name of the buffer as well as the buffer itself.
        返回模块缓冲区迭代器，生成缓冲区名称和缓冲区值
        Args:
            prefix (str): prefix to prepend to all buffer names.
            recurse (bool): if True, then yields buffers of this module
                and all submodules. Otherwise, yields only buffers that
                are direct members of this module.
            如果为True，生成该模块和所有子模块的buffers迭代器，如果为False，仅返回当前模块

        Yields:
            (string, torch.Tensor): Tuple containing the name and buffer

        Example::

            >>> for name, buf in self.named_buffers():
            >>>    if name in ['running_var']:
            >>>        print(buf.size())

        """
        gen = self._named_members(
            lambda module: module._buffers.items(),
            prefix=prefix, recurse=recurse)
        for elem in gen:
            yield elem

    def children(self) -> Iterator['Module']:
        r"""Returns an iterator over immediate children modules.
        返回直接子模块上的迭代器。

        Yields:
            Module: a child module
        """
        for name, module in self.named_children():
            yield module

    def named_children(self) -> Iterator[Tuple[str, 'Module']]:
        r"""Returns an iterator over immediate children modules, yielding both
        the name of the module as well as the module itself.
        直接返回包含子模块的迭代器，生成模块名和模块本身。


        Yields:
            (string, Module): Tuple containing a name and child module
            包含子模块名称和子模块的元组

        Example::

            >>> for name, module in model.named_children():
            >>>     if name in ['conv4', 'conv5']:
            >>>         print(module)

        """
        memo = set()
        for name, module in self._modules.items():
            if module is not None and module not in memo:
                memo.add(module)
                yield name, module

    def modules(self) -> Iterator['Module']:
        r"""Returns an iterator over all modules in the network.
        返回包含网络中所有模块的一个迭代器。
        **迭代器的意思就是可以被for循环访问！**而经过for循环后得到的就是一个又一个的tuple！

        Yields:
            Module: a module in the network

        Note:
            Duplicate modules are returned only once. In the following
            example, ``l`` will be returned only once.
            重复的模块只返回一次。在下面的示例中，``l``将只返回一次。

        Example::

            >>> l = nn.Linear(2, 2)
            >>> net = nn.Sequential(l, l)
            >>> for idx, m in enumerate(net.modules()):
                    print(idx, '->', m)

            0 -> Sequential(
              (0): Linear(in_features=2, out_features=2, bias=True)
              (1): Linear(in_features=2, out_features=2, bias=True)
            )
            1 -> Linear(in_features=2, out_features=2, bias=True)

        """
        for _, module in self.named_modules():
            yield module

    def named_modules(self, memo: Optional[Set['Module']] = None, prefix: str = '', remove_duplicate: bool = True):
        r"""Returns an iterator over all modules in the network, yielding
        both the name of the module as well as the module itself.
        返回所有模块的迭代器，生成模块名和模块本身。

        Args:
            memo: a memo to store the set of modules already added to the result
                    一个集合，用于存储已经添加的模块
            prefix: a prefix that will be added to the name of the module
                    添加到该模块的名称前缀
            remove_duplicate: whether to remove the duplicated module instances in the result
                or not
                是否删除重复的模块实例结果，默认Ture

        Yields:
            (string, Module): Tuple of name and module

        Note:
            Duplicate modules are returned only once. In the following
            重复的模块只返回一次。
            example, ``l`` will be returned only once.

        Example::

            >>> l = nn.Linear(2, 2)
            >>> net = nn.Sequential(l, l)
            >>> for idx, m in enumerate(net.named_modules()):
                    print(idx, '->', m)

            0 -> ('', Sequential(
              (0): Linear(in_features=2, out_features=2, bias=True)
              (1): Linear(in_features=2, out_features=2, bias=True)
            ))
            1 -> ('0', Linear(in_features=2, out_features=2, bias=True))

        """

        if memo is None:
            memo = set()
        if self not in memo:
            if remove_duplicate:
                memo.add(self)
            yield prefix, self # 返回当前模块信息
            for name, module in self._modules.items():
                if module is None:
                    continue
                submodule_prefix = prefix + ('.' if prefix else '') + name
                # 这里使用的module.named_modules不是self.named_modules(module是self的子模块)
                # 子模块重复“yield prefix, self”之前的操作，返回所有子模块信息
                for m in module.named_modules(memo, submodule_prefix, remove_duplicate):
                    yield m # 返回子模块信息
    """
        训练与测试

        nn.Module 通过 self.training 来区分训练和测试两种状态，使得模块可以在训练和测试时有不同的 forward 行为（如 Batch Normalization）。
        nn.Module 通过 self.train() 和 self.eval() 来修改训练和测试状态，其中 self.eval 直接调用了 self.train(False)，
        而 self.train() 会修改 self.training 并通过 self.children() 来调整所有子模块的状态。

    """
    def train(self: T, mode: bool = True) -> T:
        r"""Sets the module in training mode.
            将模块设置为训练模式。

        This has any effect only on certain modules. See documentations of
        particular modules for details of their behaviors in training/evaluation
        mode, if they are affected, e.g. :class:`Dropout`, :class:`BatchNorm`,
        etc.
        这只对某些模块有效。如果他们在训练/评估模式中受到影响，
        请参阅特定模块的文档以了解他们的行为细节，如Dropout, BatchNorm等。

        Args:
            mode (bool): whether to set training mode (``True``) or evaluation
                         mode (``False``). Default: ``True``.
                         是否设置训练模式(True)或评估模式(False)。默认值:真的。

        Returns:
            Module: self
        """
        if not isinstance(mode, bool):
            raise ValueError("training mode is expected to be boolean")
        self.training = mode
        for module in self.children():# 保证子模块的模式和父模块一致
            module.train(mode)
        return self

    def eval(self: T) -> T:
        r"""Sets the module in evaluation mode.
        将模块设置为评估模式。

        This has any effect only on certain modules. See documentations of
        particular modules for details of their behaviors in training/evaluation
        mode, if they are affected, e.g. :class:`Dropout`, :class:`BatchNorm`,
        etc.
        这只对某些模块有效。如果他们在训练/评估模式中受到影响，
        请参阅特定模块的文档以了解他们的行为细节，如Dropout, BatchNorm等。

        This is equivalent with :meth:`self.train(False) <torch.nn.Module.train>`.
        同时这和 self.train(False) 是同等的！

        See :ref:`locally-disable-grad-doc` for a comparison between
        `.eval()` and several similar mechanisms that may be confused with it.

        Returns:
            Module: self
        """
        return self.train(False)

    """
        梯度的处理
        
        对于梯度的处理 nn.Module 有两个相关的函数实现，分别是 requires_grad_ 和 zero_grad 函数，
        他们都调用了 self.parameters() 来访问所有的参数，并修改参数的 requires_grad 状态 或者 清理参数的梯度。
    """
    def requires_grad_(self: T, requires_grad: bool = True) -> T:
        r"""Change if autograd should record operations on parameters in this
        module.
            更改autograd是否应该记录此模块中参数的操作。

        This method sets the parameters' :attr:`requires_grad` attributes
        in-place.
        该方法就地设置参数的requires_grad属性。

        This method is helpful for freezing part of the module for finetuning
        or training parts of a model individually (e.g., GAN training).
        这种方法有助于冻结模块的一部分，以便对模型的各个部分进行微调或单独训练(如GAN训练)。

        See :ref:`locally-disable-grad-doc` for a comparison between
        `.requires_grad_()` and several similar mechanisms that may be confused with it.
        请参阅Locally disabling gradient computation 来比较**.requires_grad_()**和几个可能与之混淆的类似机制。

        Args:
            requires_grad (bool): whether autograd should record operations on
                                  parameters in this module. Default: ``True``.
            autograd是否应该记录对该模块中的参数的操作。默认值:真的。
        Returns:
            Module: self
        """
        for p in self.parameters():
            p.requires_grad_(requires_grad)
        return self

    def zero_grad(self, set_to_none: bool = False) -> None:
        r"""Sets gradients of all model parameters to zero. See similar function
        under :class:`torch.optim.Optimizer` for more context.
        设置所有模型参数的梯度为零。更多内容请查看torch. optim. optimizer下的类似函数。

        Args:
            set_to_none (bool): instead of setting to zero, set the grads to None.
                See :meth:`torch.optim.Optimizer.zero_grad` for details.
            set_to_none (bool) -不设置为零，而是将梯度值设置为None。详见torch. optimizer . optimizer .zero_grad()。
        """
        if getattr(self, '_is_replica', False):
            warnings.warn(
                "Calling .zero_grad() from a module created with nn.DataParallel() has no effect. "
                "The parameters are copied (in a differentiable manner) from the original module. "
                "This means they are not leaf nodes in autograd and so don't accumulate gradients. "
                "If you need gradients in your forward method, consider using autograd.grad instead.")

        for p in self.parameters():
            if p.grad is not None:
                if set_to_none:
                    p.grad = None
                else:
                    if p.grad.grad_fn is not None:
                        p.grad.detach_()
                    else:
                        p.grad.requires_grad_(False)
                    p.grad.zero_()

    def share_memory(self: T) -> T:
        r"""See :meth:`torch.Tensor.share_memory_`"""
        return self._apply(lambda t: t.share_memory_())

    def _get_name(self):
        return self.__class__.__name__

    def extra_repr(self) -> str:
        r"""Set the extra representation of the module

        To print customized extra information, you should re-implement
        this method in your own modules. Both single-line and multi-line
        strings are acceptable.
        """
        return ''

    def __repr__(self):
        # We treat the extra repr like the sub-module, one item per line
        extra_lines = []
        extra_repr = self.extra_repr()
        # empty string will be split into list ['']
        if extra_repr:
            extra_lines = extra_repr.split('\n')
        child_lines = []
        for key, module in self._modules.items():
            mod_str = repr(module)
            mod_str = _addindent(mod_str, 2)
            child_lines.append('(' + key + '): ' + mod_str)
        lines = extra_lines + child_lines

        main_str = self._get_name() + '('
        if lines:
            # simple one-liner info, which most builtin Modules will use
            if len(extra_lines) == 1 and not child_lines:
                main_str += extra_lines[0]
            else:
                main_str += '\n  ' + '\n  '.join(lines) + '\n'

        main_str += ')'
        return main_str

    def __dir__(self):
        """
            重载的 __dir__ 函数会将 self._modules、self._parameters 和 self._buffers 中的 attributes 给暴露出来。
        """
        module_attrs = dir(self.__class__)
        attrs = list(self.__dict__.keys())
        parameters = list(self._parameters.keys())
        modules = list(self._modules.keys())
        buffers = list(self._buffers.keys())
        keys = module_attrs + attrs + parameters + modules + buffers

        # Eliminate attrs that are not legal Python variable names
        keys = [key for key in keys if not key[0].isdigit()]

        return sorted(keys)

    def _replicate_for_data_parallel(self):
        replica = self.__new__(type(self))
        replica.__dict__ = self.__dict__.copy()

        # replicas do not have parameters themselves, the replicas reference the original
        # module.
        replica._parameters = OrderedDict()
        replica._buffers = replica._buffers.copy()
        replica._modules = replica._modules.copy()
        replica._is_replica = True  # type: ignore[assignment]

        return replica