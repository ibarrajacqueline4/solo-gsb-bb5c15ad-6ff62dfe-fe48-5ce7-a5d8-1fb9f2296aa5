"""静态依赖影响分析。

不执行任何文档求值、不读写决策结果：仅基于编译期确定的 depends_on
与条件表达式引用，在反向依赖图上做传递闭包（按层 BFS），回答
“这批规则发生变化会波及哪些规则”：

- level 0：输入规则本身（input_rules）；
- level 1：直接依赖输入规则的规则（direct_dependents）；
- level ≥ 2：经传递依赖间接受影响的规则（transitive_dependents）。

此外，与受影响规则同属一个 conflict_group 的其余规则不在依赖闭包内，
但命中集合变化可能改变组内冲突策略的结果，标记为联动项（coupled_rules）。
"""

from __future__ import annotations

from typing import Any, Iterable

from .compiler import CompiledRuleset


def dependency_impact(compiled: CompiledRuleset, rule_codes: list[str]) -> dict[str, Any]:
    """计算 rule_codes 的反向传递依赖闭包与冲突组联动项。

    所有受影响规则列表按 evaluation_order 排序；level 为到输入规则集合的
    最短距离（BFS 层数）。调用方需保证 rule_codes 中的编码均已编译存在。
    """
    order = compiled.evaluation_order
    order_pos = {code: index for index, code in enumerate(order)}

    def by_order(codes: Iterable[str]) -> list[str]:
        return sorted(codes, key=order_pos.__getitem__)

    # 反向邻接：被依赖者 -> 依赖它的规则。depends_on 与表达式引用取并集
    # （编译期已强制 引用 ⊆ depends_on，取并集用于兼容旧快照数据）
    reverse: dict[str, set[str]] = {code: set() for code in compiled.by_code}
    for rule in compiled.rules:
        for dep in set(rule.depends_on) | set(rule.referenced_rules):
            if dep in reverse:
                reverse[dep].add(rule.code)

    inputs = list(dict.fromkeys(rule_codes))  # 去重保序

    # 按层 BFS：level 为到输入集合的最短距离；
    # via 记录最短路径上的直接来源（上一层规则）
    level: dict[str, int] = {}
    via: dict[str, set[str]] = {}
    frontier = list(inputs)
    visited = set(inputs)
    depth = 0
    while frontier:
        depth += 1
        wave: list[str] = []
        for code in frontier:
            for dependent in sorted(reverse[code]):
                if dependent in visited:
                    if level.get(dependent) == depth:
                        via[dependent].add(code)
                    continue
                visited.add(dependent)
                level[dependent] = depth
                via[dependent] = {code}
                wave.append(dependent)
        frontier = wave

    # 冲突组 -> 成员（成员顺序即评估序）
    group_members: dict[str, list[str]] = {}
    for rule in compiled.rules:
        if rule.conflict_group:
            group_members.setdefault(rule.conflict_group, []).append(rule.code)

    affected = set(level)  # 依赖闭包（不含输入规则本身）
    impacted = set(inputs) | affected

    # 联动项：不在依赖闭包内，但与某个受影响规则同属一个冲突组
    coupled_rules: list[dict[str, Any]] = []
    coupled_codes: set[str] = set()
    for group, members in group_members.items():
        insiders = [m for m in members if m in impacted]
        outsiders = [m for m in members if m not in impacted]
        if not insiders or not outsiders:
            continue
        for code in by_order(outsiders):
            coupled_rules.append(
                {
                    "rule_code": code,
                    "conflict_group": group,
                    "coupled_with": by_order(insiders),
                }
            )
            coupled_codes.add(code)

    universe = impacted | coupled_codes

    def is_coupled(code: str) -> bool:
        """同冲突组内还存在其他受影响/联动规则时为 True。"""
        group = compiled.by_code[code].conflict_group
        if not group:
            return False
        return any(m != code and m in universe for m in group_members[group])

    def entry(code: str, rule_level: int) -> dict[str, Any]:
        return {
            "rule_code": code,
            "level": rule_level,
            "via": by_order(via.get(code, ())),
            "conflict_group": compiled.by_code[code].conflict_group,
            "coupled": is_coupled(code),
        }

    return {
        "input_rules": [entry(code, 0) for code in inputs],
        "direct_dependents": [
            entry(code, 1) for code in by_order(c for c in affected if level[c] == 1)
        ],
        "transitive_dependents": [
            entry(code, level[code])
            for code in by_order(c for c in affected if level[c] >= 2)
        ],
        "coupled_rules": coupled_rules,
    }
