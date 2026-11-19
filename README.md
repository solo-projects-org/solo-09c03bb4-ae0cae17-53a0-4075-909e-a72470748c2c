# 公交行驶轨迹整理（路段匹配）

把一辆公交车一次行程的定位点，整理成可用于路段运行时间分析的有向路段行驶轨迹。
提供 FastAPI + SQLite 网页：上传有向路段 CSV 与定位点 CSV、设置匹配半径、查看逐点归属与路段序列。

## 规则（与需求一一对应）

- 每个定位点只在**匹配半径内**的路段中取候选（点到有限线段的最短距离 ≤ 半径）。
- 相邻两点的路段只能：
  1. 保持同一路段；或
  2. 沿行驶方向进入下一段，且**上一段终点坐标 = 下一段起点坐标**（端点相接）。
- 在全程可连通的候选组合中，取**点到路段距离总和最小**者。
- 若不存在连通路径 → 待核，`reason=NO_CONNECTED_PATH`；
  若最小距离和对应**多条**路径 → 待核，`reason=AMBIGUOUS`；
  若某点半径内无路段 → 待核，`reason=NO_CANDIDATE`。
  三种情况都**不发布唯一轨迹**。
- 并列判定使用严格容差（绝对 1e-9 m + 相对 1e-12）；差在这个范围外即视为唯一。
- 端点相接的坐标比较按 1e-6 m 量化，避免浮点噪声。

## 修订与失效

- 路网、定位点各自维护递增修订号（r1、r2 …），每次上传是**整版替换**并立即成为当前版本；历史修订保留供追溯。
- 新版受理的同一事务里，依赖旧版的所有已发布结果被标记失效（`NETWORK_REPLACED` / `POINTS_REPLACED`），页面单独列出且标注“不得用于路段运行时间分析”。
- 改半径（`RADIUS_CHANGED`）同样使旧结果失效并安排重算。
- 重算任务的发布动作会再次核对“路网修订 / 定位点修订 / 半径”是否仍为当前值；不一致则任务作废（`SOURCE_CHANGED_DURING_RECOMPUTE`），绝不发布旧结果。
- 计算失败的任务回到 `pending` 并有指数退避重试；进程重启时把遗留的 `running` 任务重置为 `pending`，因此失败或重启后仍处于待重算状态，不会丢失。

## 本地运行

```bash
python3 -m pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
# 打开 http://localhost:8000
```

数据库默认在 `./data/track_matcher.sqlite3`，可用 `TRACK_MATCHER_DB` 覆盖。
`TRACK_MATCHER_WORKER=0` 只关闭后台计算线程（测试用）。

## Docker Compose

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在命名卷 `track_matcher_data`。容器内为单进程（后台计算线程与 Web 在同一进程），SQLite 文件位于卷上。

## 自测

```bash
python3 -m pytest -q
```

测试覆盖：平行路段择优、精确并列的歧义、反向/跳段不可连通、半径边界、以及“定位点更正 → 旧结果失效 → 重算 → 路网更正 → 再重算”的交错流程。

## 示例文件

`sample/` 下：

- `network.csv` + `points.csv`：两条平行路段，点更靠近北侧，应发布唯一轨迹 `N101 → N102 → N301`。
- `network_parallel.csv` + `points_parallel.csv`：两条互不相接的平行路，恰好形成并列最优 → 待核。

## 验证状态

已在本机（Python 3.11）实际执行：

- `python3 -m pytest -q` → **41 passed**：匹配算法单元测试、版本/失效/任务状态测试、HTTP 端到端测试、后台线程（含失败重试）测试、网格索引与暴力算法对拍。
- `uvicorn app.main:app` 实际启动，用 `curl` 上传样例：平行路段得到 `N101 → N102 → N301`；互不相接的平行路得到待核（并列 2 条）；换版后旧结果页显示“不得用于路段运行时间分析”；手工插入 `running` 任务后重启进程，日志出现 `reset 1 interrupted job(s) to pending` 并被重新计算完成。
- `docker-compose.yml` 仅做了 YAML 解析校验；**本环境没有 docker 命令，未实际构建镜像或 `docker compose up`**，因此容器启动通路属于未验证项。
- 单进程内使用后台线程计算，未做多副本/多 worker 水平扩展；大文件目前是全量读入内存解析（上传上限默认 20 MB）。

