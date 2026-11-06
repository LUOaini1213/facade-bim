# 完整交付入口：细节

使用已经安装 `requirements.txt` 的虚拟环境解释器运行以下命令；所有子步骤沿用这个解释器。

```powershell
.\.venv\Scripts\python.exe scripts\delivery.py --check
.\.venv\Scripts\python.exe scripts\delivery.py --revalidate
.\.venv\Scripts\python.exe scripts\delivery.py --rebuild
```

`--check` 离线核对完整交付索引并执行独立模型、IFC、IDS、净距及回放检查，不启动 Rhino 或重写正式报告。
`--rebuild` 在有许可证的 Windows Rhino 中修补允许的端缝参数，刷新数据、IFC、IDS、完整质量报告和正式日期回放，最后通过全部检查才标记交付成功。其他源参数变化会在修补前拒绝，须明确重建源模型及其输入基线。

`model/delivery_index.json` 绑定代码、配置、源模型与全部正式产物，记录依赖及 Rhino 版本。文本按统一换行计算指纹；源模型和图片按原始字节核对。输入或任一产物改变都会使旧索引失效，中途失败保留失败状态。`--manifest-only` 供 CI 在独立检查之后核对指纹。

仅代码验证改进、默认正式产物不变时，可用 `--revalidate` 完整只读复算数据、重导 IFC 并核验原生质量/回放报告。
它先要求全部原产物指纹一致，所有检查成功且过程中代码、产物和索引均未变化，才追加 `read_only_verification` 当前源码快照和验证环境。
原 `inputs`、生成环境、生成时间与产物身份全部保留；该记录表示现有交付经当前代码验证，未运行新的 Rhino 建模或渲染。
正式产物有变化时此模式会拒绝，仍须原生重建。之后 `--check` 同时检查当前验证快照和原正式产物指纹。
本次安装容量修复及独立只读核验的范围见 [验证说明](INSTALL_CAPACITY_REVIEW.md)。

## 已有源上修补压顶端缝

已有模型可用 `python scripts/run_roof_repair.py --end-joints` 按配置原生修补压顶端缝和真实铝板用量，无需重建板块身份；
脚本首先验证 `model/source_config.json` 的源 SHA 与非端缝配置基线，并逐一核对全部板块尺寸、属性和实际变换。
只有 `COPING_END_JOINT` 可以在已有源上修补；更改 `JOINT`、`MODULE`、板厚、密度或其它输入会在任何模型写入前失败，
需要明确重建源模型。不要手工修改基线文件来绕过检查。
允许更新的压顶数量和吊装重量从同一份源 `w_mm` / `h_mm` 与目标跨度计算，不与重新生成的另一套尺寸混用。
通过前置检查后，脚本先备份、逐对象核对身份与属性，读回候选文件并检查真实转角实体，再替换源文件和更新源绑定记录。
重复运行会校验已修正状态。修补后重导 IFC，并刷新质量报告及日期快照。
