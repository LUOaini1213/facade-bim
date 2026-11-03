# 三维质量与信息交付检查：细节

README 只保留结论数字（由 `scripts/check_readme.py` 回算）；这里是方法、分类口径和反例。

`rhino/quality_check.py` 对源模型中每个板块的实际块定义 Brep 应用实例变换，并加入结构楼板和转角立柱。
世界坐标包络只筛候选，碰撞最终由闭合实体布尔交集体积判断；净距用 Rhino `MeshClash` 在实际三角网格上检测。
默认净距阈值为 10 mm，可以调整。报告分开列出体积穿透、接触、低于净距阈值的事件，以及无法可靠计算的未决项，
每项含源构件编号、GUID、零件编号和毫米位置。接触需要按连接意图复核，网格见证点不表示精确最短距离。
两个源 Brep 都经顶点、直边与无孔平面证明为轴向盒时，报告另给出模型精确净距；
小于阈值、恰在阈值和接触分别归类，数值比较只使用 1e-7 mm 浮点误差预算，不借用布尔公差放宽净距。
旧模型的 98 条零间隙接触（同立面屋顶邻压顶86条、四角压顶4条、压顶与角柱8条）通过实际端缝修复消除，没有批准接触或降低阈值。
当前完整源模型的穿透、净距不足、接触、待核、未决与阈值边界计数见 README「三维质量与信息交付检查」（由 `scripts/check_readme.py` 对 `model/quality/native_all.json` 回算）。
非盒几何只产生距离待核的网格候选，不声称精确最短距；未认证曲面即使网格搜索无命中仍保留待核。
认证平面网格的无命中搜索按两侧合计 2e-6 mm 边界误差预算扩大；阈值边界单独记录，未批准接触、实际不足与待核仍阻塞净距通过。
这些是模型几何判断，不是现场实测或施工公差验收。独立检查器重新证明源盒、重算距离与完整候选集合，
改标签、改距离或删接触记录都不能使报告通过。
同一板块内部的设计连接不在构件间检查范围内；检查面向当前实际模型的静态几何，可按安装日期筛选已装板块。

```bash
python scripts/run_quality.py                         # 原生 Rhino：几何 + 实际 Eto 控件/定时器事件
python scripts/run_quality.py --mode spatial --clearance-mm 15 --date 2026-11-23
python scripts/run_quality.py --mode timeline
python scripts/check_quality.py                       # 无需 Rhino，独立核对报告与源模型身份/完整范围
python scripts/check_ids.py                           # IDS 1.0 规则 + JSON/HTML 信息交付报告
```

原生报告在 `model/quality/native_*.json`，独立检查生成同名 HTML；信息交付规则在 `quality/delivery.ids`，
其可维护配置是 `quality/profile.json`，用 `python scripts/check_ids.py --write-rules` 重新生成。
IDS 检查父板编号、板型、日期和架号，实际分件的材料及 Rhino 身份，以及正式任务和施工计划的必填信息。
压顶另检查构造包络角色、物理尺寸和质量；压顶适用范围按稳定零件名称确定，删除角色字段不能逃避适用规则。
`model/quality/ids_report.{json,html}` 保存结果。IDS 规则通过 XML schema 校验；信息检查和几何检查分别执行。
只读交付核对可给 `check_ids.py` 和 `check_quality.py` 加 `--no-write-reports`，不会重写已提交的报告。
测试会删除编号/材料、写入错误日期及提供空模型，要求失败；原生几何反例覆盖体积穿透、完全包含、
相同平面投影但不同高度、净距阈值、仅接触以及包络重叠但物体位于孔洞中。
时间轴原生检查实际创建 Eto 窗口、驱动滑块事件和真实定时器，并检查反向跳转、暂停、查询、无效输入和关闭。
导出重入检查仍真实保存模型、图片和日期统计，但 QA 使用独立临时目录；测试标注不会进入正式日期快照。
质量检查入口逐文件核对正式回放产物的运行前后 SHA-256，要求全部保持不变。

实现参考：[Rhino Eto 非模态窗口](https://developer.rhino3d.com/en/guides/eto/forms-and-dialogs/)、
[Rhino MeshClash](https://mcneel.github.io/rhinocommon-api-docs/api/RhinoCommon/html/M_Rhino_Geometry_Intersect_MeshClash_Search_4.htm)、
[IfcTester / IDS](https://docs.ifcopenshell.org/ifctester.html)。

## 历史：98 条零间隙接触从哪来

上文「旧模型的 98 条零间隙接触（86 + 4 + 8）」可以在提交 `6c2f021` 的
`model/quality/native_all.json` 里核对：`spatial.clearance_counts.contact = 98`，其中同立面相邻屋顶压顶 86 条、
四角两压顶 4 条、其余 8 条一端为压顶、另一端为转角立柱。提交 `f31339a`（PR #3）修复端缝后，当前报告的接触数为 0。

```bash
git show 6c2f021:model/quality/native_all.json | python -c "import json,sys; print(json.load(sys.stdin)['spatial']['clearance_counts'])"
```
