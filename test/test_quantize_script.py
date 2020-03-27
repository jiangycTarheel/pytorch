import torch
import torch.jit

from torch.quantization._quantize_script import script_qconfig
from torch.quantization._quantize_script import prepare_dynamic_script
from torch.quantization import default_qconfig

from torch.testing._internal.common_utils import run_tests
from torch.testing import FileCheck
from torch.testing._internal.jit_utils import attrs_with_prefix
from torch.testing._internal.jit_utils import JitTestCase, get_forward, get_forward_graph, get_module_method

from torch.jit._recursive import wrap_cpp_module
class TestScript(JitTestCase):
    def test_prepare_dynamic(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.fc = torch.nn.Linear(5, 5)

            def forward(self, x):
                return self.fc(x)

        m = torch.jit.script(M())
        m = prepare_dynamic_script(m, {'': script_qconfig(default_qconfig)})

        # for input of FC for dynamic quant
        assert len(attrs_with_prefix(m, '_observer_')) == 1
        # for weight
        assert len(attrs_with_prefix(m.fc, '_observer_')) == 1
        FileCheck().check('Observer = prim::GetAttr[name="_observer_') \
                   .check('prim::GetAttr[name="fc"]') \
                   .check('prim::CallMethod') \
                   .check_not('Observer = prim::GetAttr[name="_observer_') \
                   .run(m.graph)


    def test_prepare_dynamic_child_qconfig(self):
        class Sub(torch.nn.Module):
            def __init__(self):
                super(Sub, self).__init__()
                self.fc = torch.nn.Linear(5, 5)

            def forward(self, x):
                return self.fc(x)

        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv = torch.nn.Conv2d(3, 5, 3)
                self.sub = Sub()

            def forward(self, x):
                return self.sub(self.conv(x))

        m = torch.jit.script(M())
        # only quantize child module.
        m = prepare_dynamic_script(m, {'sub.fc': script_qconfig(default_qconfig)})

        # input of sub for dynamic quant
        assert len(attrs_with_prefix(m, '_observer_')) == 1
        # not quantized
        assert len(attrs_with_prefix(m.conv, '_observer_')) == 0
        # no observers since we observe in the outer most call site
        assert len(attrs_with_prefix(m.sub, '_observer_')) == 0
        # weight of linear
        assert len(attrs_with_prefix(m.sub.fc, '_observer_')) == 1
        FileCheck().check('prim::GetAttr[name="sub') \
                   .check('prim::CallMethod') \
                   .check('Observer = prim::GetAttr[name="_observer_') \
                   .check('prim::CallMethod') \
                   .check_not('Observer = prim::GetAttr[name="_observer_') \
                   .run(m.graph)


    def test_insert_quant_dequant_dynamic(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv = torch.nn.Conv2d(3, 5, 3).float()

            def forward(self, x):
                return self.conv(x)

        m = torch.jit.script(M())
        qconfig = default_qconfig
        qconfig_dict = {
            '': script_qconfig(qconfig)
        }
        m = wrap_cpp_module(torch._C._jit_pass_insert_observers(m._c, "forward", qconfig_dict, False, True))
        data = torch.randn(1, 3, 10, 10, dtype=torch.float)

        get_forward(m._c)(data)

        m = wrap_cpp_module(torch._C._jit_pass_insert_quant_dequant(m._c, "forward", False, True))

        assert len(m._modules._c.items()) == 1, \
            'Expected to have single submodule of conv'

        get_forward(m._c)(data)
        quant_func = "aten::quantize_per_tensor"

        # quantizing activations
        FileCheck().check("aten::_choose_qparams_per_tensor") \
                   .check(quant_func) \
                   .check("prim::CallMethod[name=\"forward\"]") \
                   .check_not(quant_func) \
                   .check("return") \
                   .run(str(get_forward_graph(m._c)))
        # quantizing weight in forward function of conv module, no choose_qparams
        FileCheck().check_not("aten::_choose_qparams_per_tensor") \
                   .check(quant_func) \
                   .check("prim::CallMethod[name=\"_conv_forward\"]") \
                   .check_not(quant_func) \
                   .check("return") \
                   .run(str(get_forward_graph(m.conv._c)))
        # shouldn't have quant/dequant in _conv_foward function
        FileCheck().check_not(quant_func) \
                   .check("aten::conv2d") \
                   .check_not(quant_func) \
                   .check("return") \
                   .run(str(get_module_method(m, 'conv', '_conv_forward').graph))

    def test_insert_quant_dequant_linear_dynamic(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.fc1 = torch.nn.Linear(5, 5).float()
                self.fc2 = torch.nn.Linear(5, 5).float()

            def forward(self, x):
                x = self.fc1(x)
                return self.fc2(x)

        m = torch.jit.script(M())
        qconfig = default_qconfig
        qconfig_dict = {
            '': script_qconfig(qconfig)
        }
        m = wrap_cpp_module(torch._C._jit_pass_insert_observers(m._c, "forward", qconfig_dict, False, True))
        data = torch.randn(5, 5, dtype=torch.float)

        get_forward(m._c)(data)

        m = wrap_cpp_module(torch._C._jit_pass_insert_quant_dequant(m._c, "forward", False, True))

        assert len(m._modules._c.items()) == 2, \
            'Expected to have two submodule of linear'

        get_forward(m._c)(data)
        quant_func = "aten::quantize_per_tensor"

        # quantizing activations
        FileCheck().check("aten::_choose_qparams_per_tensor") \
                   .check(quant_func) \
                   .check("prim::CallMethod[name=\"forward\"]") \
                   .check("aten::_choose_qparams_per_tensor") \
                   .check(quant_func) \
                   .check("prim::CallMethod[name=\"forward\"]") \
                   .check_not(quant_func) \
                   .check("return") \
                   .run(str(get_forward_graph(m._c)))
        # quantizing weight in forward function of fc module, no choose_qparams
        FileCheck().check_not("aten::_choose_qparams_per_tensor") \
                   .check(quant_func) \
                   .check("prim::CallFunction") \
                   .check_not(quant_func) \
                   .check("return") \
                   .run(str(get_forward_graph(m.fc1._c)))

if __name__ == "__main__":
    run_tests()
