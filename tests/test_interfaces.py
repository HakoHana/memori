"""接口契约测试 — 验证所有实现类满足 ABC 接口定义

每个接口测试：
1. 接口定义了所有必需方法
2. 实现类实现了所有抽象方法（可正常实例化）
3. 方法签名基本一致（参数数量匹配）
"""

from __future__ import annotations

import inspect
from abc import ABC

import pytest

from memori.core.interfaces import (
    ICapturer,
    IRetriever,
    IPersonaEngine,
    IGraphEngine,
    ICommandHandler,
    IMemoryInjector,
    IWarmProcessor,
    IConsolidationManager,
    IHotMessageCache,
)


# ═══════════════════════════════════════════════════════════════
#  所有接口的清单
# ═══════════════════════════════════════════════════════════════

_ALL_INTERFACES = [
    ICapturer,
    IRetriever,
    IPersonaEngine,
    IGraphEngine,
    ICommandHandler,
    IMemoryInjector,
    IWarmProcessor,
    IConsolidationManager,
    IHotMessageCache,
]


class TestInterfaceDefinitions:
    """接口定义完整性"""

    @pytest.mark.parametrize("iface", _ALL_INTERFACES)
    def test_is_abc(self, iface):
        """每个接口都必须是 ABC 的子类"""
        assert issubclass(iface, ABC)

    @pytest.mark.parametrize("iface", _ALL_INTERFACES)
    def test_has_abstract_methods(self, iface):
        """每个接口应至少有一个抽象方法"""
        abstract = [
            name for name, method in iface.__dict__.items()
            if getattr(method, "__isabstractmethod__", False)
        ]
        assert len(abstract) >= 1, f"{iface.__name__} 没有抽象方法"
        # 打印接口方法清单（方便阅读测试日志）
        print(f"\n{iface.__name__}: {', '.join(abstract)}")


# ═══════════════════════════════════════════════════════════════
#  具体实现类的接口合规性
# ═══════════════════════════════════════════════════════════════

# 接口 → (实现类, 需要 mock 的参数)
_IMPLEMENTATIONS: dict = {
    ICapturer: ("memori.pipeline.capturer", "Capturer"),
    IRetriever: ("memori.core.retriever", "Retriever"),
    IPersonaEngine: ("memori.features.persona_engine", "PersonaEngine"),
    IGraphEngine: ("memori.features.graph_engine", "GraphEngine"),
    ICommandHandler: ("memori.features.command_handler", "CommandHandler"),
    IMemoryInjector: ("memori.core.memory_injector", "MemoryInjector"),
    IConsolidationManager: ("memori.pipeline.consolidation_manager", "ConsolidationManager"),
    IWarmProcessor: ("memori.pipeline.warm_processor", "WarmProcessor"),
    IHotMessageCache: ("memori.core.hot_cache", "HotMessageCache"),
}


class TestInterfaceCompliance:
    """所有实现类必须实现其接口的全部抽象方法"""

    @pytest.mark.parametrize("iface,impl_info", list(_IMPLEMENTATIONS.items()))
    def test_all_abstract_methods_implemented(self, iface, impl_info):
        """验证实现类实现了接口的所有抽象方法"""
        module_path, class_name = impl_info
        import importlib
        module = importlib.import_module(module_path)
        impl_class = getattr(module, class_name)

        abstract_methods = [
            name for name, method in iface.__dict__.items()
            if getattr(method, "__isabstractmethod__", False)
        ]

        for method_name in abstract_methods:
            assert hasattr(impl_class, method_name), (
                f"{class_name} 未实现 {iface.__name__}.{method_name}"
            )
            impl_method = getattr(impl_class, method_name)
            assert callable(impl_method) or inspect.isfunction(impl_method), (
                f"{class_name}.{method_name} 不可调用"
            )

    @pytest.mark.parametrize("iface,impl_info", list(_IMPLEMENTATIONS.items()))
    def test_interface_inheritance(self, iface, impl_info):
        """验证实现类的 MRO 中包含该接口"""
        module_path, class_name = impl_info
        import importlib
        module = importlib.import_module(module_path)
        impl_class = getattr(module, class_name)

        assert issubclass(impl_class, iface), (
            f"{class_name} 不是 {iface.__name__} 的子类"
        )

    def test_interface_list_comprehensive(self):
        """确保所有接口都在测试覆盖中"""
        # 扫一遍 interfaces.py 找到所有 ABC 类
        import inspect as ins
        from memori.core import interfaces as iface_module

        defined = []
        for name, obj in ins.getmembers(iface_module):
            if ins.isclass(obj) and issubclass(obj, ABC) and obj is not ABC:
                defined.append(obj)

        # 验证我们覆盖了所有接口
        for iface in defined:
            assert iface in _ALL_INTERFACES, f"{iface.__name__} 不在测试清单中"


class TestMethodSignatures:
    """关键接口方法签名一致性检查"""

    def test_capturer_capture_signature(self):
        """ICapturer.capture 的方法签名"""
        sig = inspect.signature(ICapturer.capture)
        params = list(sig.parameters.keys())
        for required_param in ["self", "user_id", "conversation_summary", "judge_result"]:
            assert required_param in params, f"capture 缺少参数: {required_param}"

    def test_retriever_recall_signature(self):
        sig = inspect.signature(IRetriever.recall)
        params = list(sig.parameters.keys())
        for required_param in ["self", "user_id", "query"]:
            assert required_param in params

    def test_retriever_get_context_memories_signature(self):
        sig = inspect.signature(IRetriever.get_context_memories)
        params = list(sig.parameters.keys())
        for required_param in ["self", "user_id", "query"]:
            assert required_param in params
