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
