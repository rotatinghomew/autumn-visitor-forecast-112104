# 团期客流预测的同场评分与值班采用

值班比较按团期场次联接容量与预测。客流是人数，时数使用所在地日期时间。

## 资料交换

`data/samples.json` 是合成样册。`fields` 给出有序列名，`rows` 按该顺序记录数据，`meaning` 标明单位或来源区别。没有填写的值用 `null` 表示。

- `sessions`：cut 按距离开场的小时数。 列：`slot`, `date`, `shift`, `school_kind`, `seats`, `peak_threshold`, `early_cut`, `intraday_cut`。
- `predictions`：概率是原条件下的出具值；没有相应阈值就没有直接概率。 列：`slot`, `source`, `ahead_hours`, `p10`, `p50`, `p90`, `threshold`, `probability_above`, `seq`。
- `gate`：等待区单列；自然结束、满员和取消含义不同。 列：`slot`, `entered`, `waiting`, `closed`, `known_seq`。
- `crews`：同一 pool 同时只能分给一个场次。 列：`shift`, `pool`, `type`, `available`, `min_keep`。
- `commitments`：已经通知的人员归属。 列：`slot`, `pool`, `amount`, `started_by`, `seq`。
- `revisions`：后补发布的容量线。 列：`slot`, `peak_threshold`, `known_seq`。

## 检视

使用 Python 3.11，资料读取不依赖第三方包。

```sh
python3 source_book.py data/samples.json
python3 -m unittest discover -s tests -v
```

资料读取命令列出各表记录数；测试检查交叉引用与格式，不生成分析结果。

## 决策点还原、同场评分与值班采用

`forecast_desk.py` 按两个决策点（早班排工、当日增援）还原当时切实可用的预报版本，
统计概率区间与超出拥挤线的风险，给出受约束的班表建议，并说明核对结果怎样影响
下一轮采用。

```sh
python3 forecast_desk.py data/samples.json            # 以册中最大公开序号为当前时点
python3 forecast_desk.py data/samples.json --at-seq 2 # 重建更早的信息时点
```

约定：

- 评分目标统一为入馆人数；寄存等候区单列，不计入。
- 出具提前量晚于决策点、或结果公开后补写的版本，不参加该决策点的统计与评分。
- 满员截断场不用确认进场人数给点误差满分，只做截断判定；迟报、取消、新开
  （未开场）分别说明观察能支持哪些评价。
- 来源比较只落在共同支撑的场次上（统一目标、统一提前量、重叠场次）；没有共同
  支撑时各自能说明的范围分开保留；同一来源两轮对照只在交集上判断真变好，
  场次集不同则整体均值不可比。
- 拥挤线修订后旧的直接超阈概率失效，只能由分位数推算，并重新判断该来源在哪些
  场次还能采用。
- 早班与增援可选用不同预报或组合，但班表满足最低保留与 pool 独占；已通知的
  早班人力不可释放为增援，余量按 可用 - 已通知 - 最低保留 明示；来源权重只在
  其核对过的场次类型与需求段内采用。
