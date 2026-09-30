"""main.py 静态自检（纯 AST，不需要 astrbot 运行时）。

跑法：uv run --no-project python tests/test_static.py

守三类「静默失效」的坑（都真发生过）：
  1. `self.<协程方法>(...)` 漏 `await` —— 协程被创建后直接丢掉，功能整个不执行，
     日志里只有一条 RuntimeWarning「coroutine was never awaited」。
     v0.1.0~v0.1.2 的「回复概率跟图」与「关键词概率触发」就是这么全程没生效的。
  2. 事件钩子（on_llm_response / on_decorating_result / after_message_sent …）不能是
     异步生成器：AstrBot 的 `call_event_hook` 里有 `assert inspect.iscoroutinefunction`，
     加了 yield 就会在运行期直接炸。
  3. 指令回执不能再走结果链（`event.plain_result / image_result / chain_result`）：
     那样会被核心 `ResultDecorateStage` 按全局「回复时 @ 发送人」插 At，
     插件撤不掉（v0.1.2 改成直发就是为了这个，别改回去）。
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "main.py"

# 走 call_event_hook 的钩子：必须是普通协程函数（不能 yield）
COROUTINE_ONLY_HOOKS = {
    "on_llm_request",
    "on_llm_response",
    "on_agent_begin",
    "on_agent_done",
    "on_decorating_result",
    "after_message_sent",
    "on_using_llm_tool",
    "on_llm_tool_respond",
    "on_plugin_error",
}

# 结果链结果（会被核心装饰、可能被插 At）
RESULT_CHAIN_CALLS = {"plain_result", "image_result", "chain_result"}


def expect(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def decorator_name(node: ast.AST) -> str:
    """取装饰器的最外层名字：@filter.on_llm_response() → on_llm_response"""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def has_yield(fn: ast.AsyncFunctionDef) -> bool:
    """函数体里（不含内嵌函数）有没有 yield。"""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue  # 内嵌函数自己的 yield 不算
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def main() -> None:
    tree = ast.parse(TARGET.read_text(encoding="utf-8"), filename=str(TARGET))

    async_methods = {
        n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
    }
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    missing_await = []
    bad_hooks = []
    chain_calls = []

    for node in ast.walk(tree):
        # 1) 漏 await
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in async_methods
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                and not isinstance(parent.get(node), ast.Await)
            ):
                missing_await.append(f"L{node.lineno} self.{func.attr}() 少了 await")
            # 3) 指令回执走了结果链
            if isinstance(func, ast.Attribute) and func.attr in RESULT_CHAIN_CALLS:
                chain_calls.append(f"L{node.lineno} {func.attr}()")

        # 2) 事件钩子不能是异步生成器
        if isinstance(node, ast.AsyncFunctionDef):
            names = {decorator_name(d) for d in node.decorator_list}
            if names & COROUTINE_ONLY_HOOKS and has_yield(node):
                bad_hooks.append(
                    f"L{node.lineno} {node.name}：钩子 {sorted(names & COROUTINE_ONLY_HOOKS)} "
                    "里有 yield（必须是普通协程）"
                )

    expect(not missing_await, "漏 await：\n  " + "\n  ".join(missing_await))
    expect(not bad_hooks, "钩子写成了异步生成器：\n  " + "\n  ".join(bad_hooks))
    expect(
        not chain_calls,
        "指令回执又走了结果链（会被核心插 At）：\n  " + "\n  ".join(chain_calls),
    )

    print("test_static: all OK")


if __name__ == "__main__":
    main()
