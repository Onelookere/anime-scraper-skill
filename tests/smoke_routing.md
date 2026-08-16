# 测试路由

测试不联网。三个主测试套件由 `tests/smoke_test.py` 在同一个 Python 进程中统一调用；低频可选工具另由 `smoke_optional.py` 单独运行。

## 套件

| 测试文件 | 负责范围 | 典型修改入口 |
|---|---|---|
| `smoke_core.py` | match/NFO 纯逻辑、标题和分集规则、人员/歌曲规则、缓存公共工具 | `scripts/match.py`、`scripts/nfo.py`、`scripts/_common.py`、`scripts/bangumi.py`、`scripts/anidb_episodes.py`、`scripts/tmdb.py` 的纯逻辑或缓存行为 |
| `smoke_integration.py` | 文件系统、硬链接、图片、CLI、scrape、bootstrap、配置落盘 | `scripts/scrape.py`、`scripts/link_library.py`、`scripts/images.py`、`scripts/artwork_review.py`、`scripts/update_artwork.py`、`scripts/library_audit.py`、`scripts/bootstrap.py` |
| `smoke_contract.py` | SKILL、references、触发边界和发布契约 | `SKILL.md`、`references/`、`tests/trigger_cases.json`、配置/契约文本 |
| `smoke_optional.py` | 音频指纹和全库审计等低频工具 | `scripts/audio_fingerprint.py`、`scripts/library_audit.py`、`references/optional-tools.md` |

`smoke_support.py` 只提供三个套件共用的导入和测试辅助函数，不是可选择运行的测试套件。

## 命令

完整验证：

```text
python scripts/bootstrap.py --run tests/smoke_test.py
```

选择性验证：

```text
python scripts/bootstrap.py --run tests/smoke_core.py
python scripts/bootstrap.py --run tests/smoke_integration.py
python scripts/bootstrap.py --run tests/smoke_contract.py
python scripts/bootstrap.py --run tests/smoke_optional.py
```

只修改单一模块时运行对应套件；修改跨越多个责任边界时运行所有受影响套件。涉及入口、公共工具、测试路由或无法明确归类的修改，直接运行完整入口。提交或交付前必须运行完整入口。
