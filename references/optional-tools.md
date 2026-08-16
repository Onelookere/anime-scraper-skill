# 可选工具与低频维护

本文件只在用户明确要求对应能力时读取；首次完整刮削不加载。

## 音频指纹（OP/ED 身份线索）

只有文件名、时长、Bangumi、AniDB、官网和可靠维基都无法对应时，才可在隔离环境显式安装 `shazamio` 后运行：

```text
python scripts/audio_fingerprint.py <media> [...]
```

脚本用 ffmpeg 截取 5–30 秒临时 WAV，只向非官方 Shazam 接口发送音频指纹，并仅返回歌名、艺人、ISRC 和识别 URL；结果只能作身份线索，必须再与官方或可靠维基交叉确认，并把 URL 写入 `song_evidence`。未确认仍用裸 OP/ED。该工具不在默认依赖中。

## 全库审计

用户明确要求全库体检时才运行 `scripts/library_audit.py`。`--library-root` 必须是本次作品的精确目录；公共 `_Jellyfin` 根只有用户明确要求全库审计时才可传入。脚本只读核对硬链接身份、空 `<plot />`、孤儿 NFO 和散落文件，不参与普通阶段四验收。

## 作品级 staff 修复

已有成果需要只修复作品级 staff 时，读取 `references/incremental.md` 并使用 `scripts/repair_staff.py`；必须显式提供 persons/characters 缓存，工具不属于首次刮削入口。

