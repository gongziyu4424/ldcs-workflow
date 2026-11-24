"""临时核验：动态最低缴费年限规则"""
from ldcs.retire_calc import calc_retirement, min_contribution_for
from ldcs.retire_calc import MALE, FEMALE_CADRE, FEMALE_WORKER

print("=== 规则点对点核验 ===")
cases = [
    (2025, 1), (2029, 12), (2030, 1), (2030, 6),
    (2031, 1), (2032, 7), (2035, 3), (2038, 12), (2039, 1), (2040, 6),
]
for y, m in cases:
    n, cn = min_contribution_for(y, m)
    print(f"  退休 {y}-{m:02d} -> {cn}（{n} 个月）")

print("=== 几组典型退休 ===")
for args in [(1965, 3, MALE), (1981, 9, FEMALE_CADRE),
             (1985, 1, FEMALE_WORKER), (1963, 6, MALE)]:
    r = calc_retirement(*args)
    print(f"  {args} 延迟后 {r['retire_cn']} 最低缴费 {r['min_contribution_cn']}")