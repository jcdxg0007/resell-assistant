import React, { useState, useEffect, useCallback } from 'react';
import {
  Typography, Card, Row, Col, Tag, Space, Button, Table, List, Input,
  Progress, Statistic, Empty, App, Tooltip, Segmented, Alert, Divider, Image,
} from 'antd';
import {
  ReloadOutlined, ThunderboltOutlined, SwapOutlined, SearchOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';

const { Title, Text } = Typography;

// ── 类型 ───────────────────────────────────────────────────────
interface KeywordEntry {
  keyword: string;
  has_pdd: boolean;
  has_xianyu: boolean;
  both: boolean;
  cached: boolean;
  scored_at: string | null;
  stale: boolean;
}

interface Dimension {
  name: string;
  score: number;
  max: number;
  label: string;
  has_data: boolean;
}

interface SideItem {
  product_id?: string;
  title: string;
  price: number;
  image_url?: string | null;
  item_wants?: number;
  sales?: number;
  badges?: string[];
  relevance: number;
  risk_tags: string[];
  total_score: number;
  decision: string;
  decision_label: string;
  dimensions: Dimension[];
}

interface SidePayload {
  platform: string;
  median: number;
  p25: number;
  p75: number;
  sample_size: number;
  active_listings?: number | null;
  items: SideItem[];
}

interface Arbitrage {
  available: boolean;
  reason?: string;
  direction?: string;
  direction_label?: string;
  sell_price?: number;
  source_cost?: number;
  estimated_cost?: number;
  estimated_profit?: number;
  profit_margin?: number;
  total_score?: number;
  decision?: string;
  decision_label?: string;
  dimensions?: Dimension[];
}

interface Analysis {
  keyword: string;
  scored_at: string | null;
  cached: boolean;
  xianyu: SidePayload | null;
  pdd: SidePayload | null;
  arbitrage: Arbitrage | null;
}

// ── 样式映射 ───────────────────────────────────────────────────
const sideDecisionColor: Record<string, string> = { buy: 'green', watch: 'orange', skip: 'red' };
const arbDecisionColor: Record<string, string> = { strong: 'green', try: 'blue', skip: 'red' };

const scoreColor = (s: number) => (s >= 75 ? '#52c41a' : s >= 55 ? '#1677ff' : s >= 40 ? '#faad14' : '#ff4d4f');

// 模块级缓存 + sessionStorage 持久化：
//  - 模块级：跨路由切换存活（组件卸载 state 会丢，模块级不会）。
//  - sessionStorage：整页刷新(F5)也能瞬间还原，不再请求后端；关掉标签页自动清理。
// 后端本身有 DB 缓存兜底，前端这层只是为了“切回来/刷新后秒显、不转圈”。
const CACHE_KEY = 'tendim_analysis_cache_v1';
const SEL_KEY = 'tendim_last_selected_v1';

function hydrateMemo(): Record<string, Analysis> {
  try {
    const raw = sessionStorage.getItem(CACHE_KEY);
    return raw ? (JSON.parse(raw) as Record<string, Analysis>) : {};
  } catch { return {}; }
}

const analysisMemo: Record<string, Analysis> = hydrateMemo();
let lastSelectedMemo: string | null = (() => {
  try { return sessionStorage.getItem(SEL_KEY); } catch { return null; }
})();

function persistMemo(): void {
  try { sessionStorage.setItem(CACHE_KEY, JSON.stringify(analysisMemo)); }
  catch { /* 容量超限等异常忽略：内存缓存仍在，后端也有 DB 兜底 */ }
}
function persistSelected(kw: string | null): void {
  try {
    if (kw) sessionStorage.setItem(SEL_KEY, kw);
    else sessionStorage.removeItem(SEL_KEY);
  } catch { /* ignore */ }
}

const fmtTime = (iso: string | null | undefined) => {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
};

// ── 维度条 ─────────────────────────────────────────────────────
const DimensionBars: React.FC<{ dims: Dimension[] }> = ({ dims }) => (
  <Space direction="vertical" size={6} style={{ width: '100%' }}>
    {dims.map((d) => (
      <div key={d.name} style={{ display: 'flex', alignItems: 'center' }}>
        <Text style={{ width: 100, fontSize: 12 }} type={d.has_data ? undefined : 'secondary'}>{d.name}</Text>
        <Progress
          percent={Math.round((d.score / d.max) * 100)}
          size="small"
          style={{ flex: 1, marginRight: 10 }}
          strokeColor={d.has_data ? undefined : '#d9d9d9'}
          format={() => `${d.score}/${d.max}`}
        />
        <Text type="secondary" style={{ fontSize: 12, width: 96, textAlign: 'right' }} ellipsis>
          {d.has_data ? d.label : `${d.label}(无数据)`}
        </Text>
      </div>
    ))}
  </Space>
);

// ── 单平台结果表 ───────────────────────────────────────────────
const SideTable: React.FC<{ side: SidePayload | null; platform: 'xianyu' | 'pdd' }> = ({ side, platform }) => {
  if (!side || !side.items?.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`暂无${platform === 'xianyu' ? '闲鱼' : 'PDD'}样本`} />;
  }
  const heatTitle = platform === 'xianyu' ? '想要' : '销量';
  const columns: ColumnsType<SideItem> = [
    {
      title: '商品', dataIndex: 'title', ellipsis: true,
      render: (t: string, r: SideItem) => (
        <Space size={6}>
          {r.image_url && (
            <Image
              src={r.image_url}
              alt=""
              width={32}
              height={32}
              style={{ objectFit: 'cover', borderRadius: 4 }}
              preview={{ mask: false }}
            />
          )}
          <Text ellipsis style={{ maxWidth: 220 }}>{t || '—'}</Text>
          {(r.risk_tags || []).includes('suspicious_low') && <Tag color="volcano">极低价</Tag>}
        </Space>
      ),
    },
    { title: '价格', dataIndex: 'price', width: 80, sorter: (a, b) => a.price - b.price, render: (v: number) => <Text>¥{v?.toFixed(0)}</Text> },
    {
      title: heatTitle, width: 70,
      render: (_: unknown, r: SideItem) => <Text type="secondary">{platform === 'xianyu' ? (r.item_wants ?? 0) : (r.sales ?? 0)}</Text>,
    },
    {
      title: '得分', dataIndex: 'total_score', width: 78, defaultSortOrder: 'descend',
      sorter: (a, b) => a.total_score - b.total_score,
      render: (s: number) => <Text strong style={{ color: scoreColor(s) }}>{s}</Text>,
    },
    {
      title: '判定', dataIndex: 'decision', width: 70,
      render: (d: string, r: SideItem) => <Tag color={sideDecisionColor[d]}>{r.decision_label}</Tag>,
    },
  ];
  return (
    <Table<SideItem>
      size="small"
      rowKey={(r, i) => r.product_id || `${platform}-${i}`}
      columns={columns}
      dataSource={side.items}
      pagination={{ pageSize: 8, size: 'small', showSizeChanger: false }}
      expandable={{
        expandedRowRender: (r) => <DimensionBars dims={r.dimensions} />,
        rowExpandable: (r) => !!r.dimensions?.length,
      }}
    />
  );
};

const TenDimSelection: React.FC = () => {
  const { message } = App.useApp();

  const [keywords, setKeywords] = useState<KeywordEntry[]>([]);
  const [kwLoading, setKwLoading] = useState(false);
  const [filter, setFilter] = useState<'both' | 'all'>('both');
  const [search, setSearch] = useState('');

  // 初值从模块级缓存恢复：切回本页时还原上次选中的词及其分析结果
  const [selected, setSelected] = useState<string | null>(lastSelectedMemo);
  const [analysis, setAnalysis] = useState<Analysis | null>(
    lastSelectedMemo ? analysisMemo[lastSelectedMemo] ?? null : null,
  );
  const [analyzing, setAnalyzing] = useState(false);
  const [sideView, setSideView] = useState<'xianyu' | 'pdd'>('xianyu');
  // 前端缓存：已加载过的词点回来秒显、不再请求后端，避免“又重新分析”
  const [analysisCache, setAnalysisCache] = useState<Record<string, Analysis>>({ ...analysisMemo });

  // 写缓存：同时落到 state（触发渲染）、模块级（跨页面）、sessionStorage（跨刷新）
  const putCache = useCallback((kw: string, data: Analysis) => {
    analysisMemo[kw] = data;
    persistMemo();
    setAnalysisCache((p) => ({ ...p, [kw]: data }));
  }, []);
  const [analyzingAll, setAnalyzingAll] = useState(false);
  const [allProgress, setAllProgress] = useState<{ done: number; total: number } | null>(null);

  const loadKeywords = useCallback(async () => {
    setKwLoading(true);
    try {
      const res = await api.get('/selection/ten-dim/keywords');
      setKeywords(res.data.items || []);
    } catch { /* 静默 */ } finally { setKwLoading(false); }
  }, []);

  const loadAnalysis = useCallback(async (kw: string) => {
    setSelected(kw);
    lastSelectedMemo = kw;
    persistSelected(kw);
    // 命中前端缓存：直接展示，不清空、不转圈、不再请求后端
    const hit = analysisCache[kw];
    if (hit) {
      setAnalysis(hit);
      setAnalyzing(false);
      return;
    }
    setAnalyzing(true);
    setAnalysis(null);
    try {
      const res = await api.get(`/selection/ten-dim/${encodeURIComponent(kw)}`);
      setAnalysis(res.data);
      putCache(kw, res.data);
    } catch {
      message.error('分析失败');
    } finally { setAnalyzing(false); }
  }, [analysisCache, putCache, message]);

  const refreshAnalysis = useCallback(async () => {
    if (!selected) return;
    setAnalyzing(true);
    try {
      const res = await api.post(`/selection/ten-dim/${encodeURIComponent(selected)}/refresh`);
      setAnalysis(res.data);
      putCache(selected, res.data);
      message.success('已重新分析');
      loadKeywords();
    } catch {
      message.error('重新分析失败');
    } finally { setAnalyzing(false); }
  }, [selected, putCache, message, loadKeywords]);

  // 全部分析：把当前列表里每个词都灌进前端缓存，跑完点哪个都秒显。
  //  - 过期 / 从没分析过 → 重算(refresh)
  //  - 只是本地缓存没有、但后端有 → 用快速 GET 拉 DB 缓存进来(不重算)
  //  - 本地已有且不过期 → 跳过
  const analyzeAll = useCallback(async (targets: KeywordEntry[]) => {
    const todo = targets.filter((k) => !analysisMemo[k.keyword] || k.stale);
    if (todo.length === 0) { message.info('当前列表都已加载且是最新，点哪个词都秒显'); return; }
    setAnalyzingAll(true);
    setAllProgress({ done: 0, total: todo.length });
    let ok = 0; let fail = 0;
    for (let i = 0; i < todo.length; i++) {
      const k = todo[i];
      const kw = k.keyword;
      try {
        const needRecompute = !k.cached || k.stale;
        const res = needRecompute
          ? await api.post(`/selection/ten-dim/${encodeURIComponent(kw)}/refresh`)
          : await api.get(`/selection/ten-dim/${encodeURIComponent(kw)}`);
        putCache(kw, res.data);
        if (kw === selected) setAnalysis(res.data);
        ok += 1;
      } catch { fail += 1; }
      setAllProgress({ done: i + 1, total: todo.length });
    }
    setAnalyzingAll(false);
    setAllProgress(null);
    message[fail ? 'warning' : 'success'](`全部分析完成：成功 ${ok}${fail ? `，失败 ${fail}` : ''}，点哪个词都秒显`);
    loadKeywords();
  }, [selected, putCache, message, loadKeywords]);

  useEffect(() => { loadKeywords(); }, [loadKeywords]);

  const visibleKeywords = keywords.filter((k) => {
    if (filter === 'both' && !k.both) return false;
    if (search && !k.keyword.includes(search.trim())) return false;
    return true;
  });

  const arb = analysis?.arbitrage;

  return (
    <Row gutter={16} style={{ height: '100%' }}>
      {/* 左：候选关键词 */}
      <Col xs={24} md={7} lg={6}>
        <Card
          title="候选关键词"
          size="small"
          extra={<Button size="small" type="text" icon={<ReloadOutlined />} onClick={loadKeywords} loading={kwLoading} />}
          styles={{ body: { padding: '8px 12px' } }}
        >
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Input
              size="small" allowClear prefix={<SearchOutlined />} placeholder="筛选关键词"
              value={search} onChange={(e) => setSearch(e.target.value)}
            />
            <Segmented
              size="small" block value={filter}
              onChange={(v) => setFilter(v as 'both' | 'all')}
              options={[{ label: '两池齐全', value: 'both' }, { label: '全部', value: 'all' }]}
            />
            <div style={{ maxHeight: 'calc(100vh - 260px)', overflowY: 'auto' }}>
              <List
                size="small"
                dataSource={visibleKeywords}
                loading={kwLoading}
                locale={{ emptyText: filter === 'both' ? '没有两池都有数据的关键词' : '暂无候选关键词' }}
                renderItem={(k) => (
                  <List.Item
                    onClick={() => loadAnalysis(k.keyword)}
                    style={{
                      cursor: 'pointer', paddingInline: 8, borderRadius: 4,
                      background: selected === k.keyword ? '#e6f4ff' : undefined,
                    }}
                  >
                    <Space direction="vertical" size={2} style={{ width: '100%' }}>
                      <Space size={4} wrap>
                        <Text strong={selected === k.keyword}>{k.keyword}</Text>
                        {k.both
                          ? <Tag color="green" style={{ marginInlineEnd: 0 }}>双平台</Tag>
                          : k.has_pdd
                            ? <Tag color="red" style={{ marginInlineEnd: 0 }}>仅PDD</Tag>
                            : <Tag color="gold" style={{ marginInlineEnd: 0 }}>仅闲鱼</Tag>}
                        {k.stale && <Tag color="orange" style={{ marginInlineEnd: 0 }}>待刷新</Tag>}
                      </Space>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {k.cached ? `已分析 ${fmtTime(k.scored_at)}` : '未分析'}
                      </Text>
                    </Space>
                  </List.Item>
                )}
              />
            </div>
          </Space>
        </Card>
      </Col>

      {/* 右：分析结果 */}
      <Col xs={24} md={17} lg={18}>
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Row justify="space-between" align="middle">
            <Col>
              <Title level={4} style={{ margin: 0 }}>
                十维度选品{selected && <Tag color="blue" style={{ marginLeft: 8 }}>{selected}</Tag>}
              </Title>
            </Col>
            <Col>
              <Space>
                {analysis?.scored_at && <Text type="secondary" style={{ fontSize: 12 }}>分析于 {fmtTime(analysis.scored_at)}</Text>}
                <Button
                  icon={<ThunderboltOutlined />} disabled={visibleKeywords.length === 0 || analyzing}
                  loading={analyzingAll} onClick={() => analyzeAll(visibleKeywords)}
                >
                  {analyzingAll && allProgress ? `分析中 ${allProgress.done}/${allProgress.total}` : '全部分析'}
                </Button>
                <Button
                  icon={<ThunderboltOutlined />} type="primary" disabled={!selected || analyzingAll}
                  loading={analyzing} onClick={refreshAnalysis}
                >
                  重新分析
                </Button>
              </Space>
            </Col>
          </Row>

          {!selected ? (
            <Card><Empty description="从左侧选择一个关键词查看 A闲鱼端 / B PDD端 / C跨平台套利 三层分析" /></Card>
          ) : (
            <>
              {/* C 跨平台套利结论 */}
              <Card
                title={<Space><SwapOutlined />跨平台套利结论</Space>}
                size="small"
                loading={analyzing}
                extra={arb?.available && <Tag color={arbDecisionColor[arb.decision || 'skip']}>{arb.decision_label}</Tag>}
              >
                {!arb ? null : !arb.available ? (
                  <Alert type="info" showIcon message={arb.reason || '两端数据不足，无法比价'} />
                ) : (
                  <Row gutter={[16, 16]}>
                    <Col xs={24} lg={10}>
                      <Space direction="vertical" size={8} style={{ width: '100%' }}>
                        <Tag color="purple" style={{ fontSize: 13, padding: '4px 10px' }}>{arb.direction_label}</Tag>
                        <Row gutter={8}>
                          <Col span={8}><Statistic title="进货成本" value={arb.source_cost} prefix="¥" valueStyle={{ fontSize: 18 }} /></Col>
                          <Col span={8}><Statistic title="卖出价" value={arb.sell_price} prefix="¥" valueStyle={{ fontSize: 18 }} /></Col>
                          <Col span={8}>
                            <Statistic
                              title="预估利润" value={arb.estimated_profit} prefix="¥"
                              valueStyle={{ fontSize: 18, color: (arb.estimated_profit || 0) > 0 ? '#52c41a' : '#ff4d4f' }}
                            />
                          </Col>
                        </Row>
                        <Space size="large">
                          <Statistic title="利润率" value={arb.profit_margin} suffix="%" valueStyle={{ fontSize: 16 }} />
                          <Statistic title="套利综合分" value={arb.total_score} suffix="/100" valueStyle={{ fontSize: 16, color: scoreColor(arb.total_score || 0) }} />
                        </Space>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          含损耗与运费后的到手成本约 ¥{arb.estimated_cost}
                        </Text>
                      </Space>
                    </Col>
                    <Col xs={24} lg={14}>
                      {arb.dimensions && <DimensionBars dims={arb.dimensions} />}
                    </Col>
                  </Row>
                )}
              </Card>

              {/* A/B 单平台排序 */}
              <Card
                size="small"
                loading={analyzing}
                title={
                  <Space>
                    <Segmented
                      value={sideView}
                      onChange={(v) => setSideView(v as 'xianyu' | 'pdd')}
                      options={[
                        { label: `闲鱼端${analysis?.xianyu ? `(${analysis.xianyu.sample_size})` : ''}`, value: 'xianyu' },
                        { label: `PDD端${analysis?.pdd ? `(${analysis.pdd.sample_size})` : ''}`, value: 'pdd' },
                      ]}
                    />
                  </Space>
                }
                extra={
                  (() => {
                    const s = sideView === 'xianyu' ? analysis?.xianyu : analysis?.pdd;
                    if (!s) return null;
                    return (
                      <Space size={12} wrap>
                        <Text type="secondary" style={{ fontSize: 12 }}>中位价 <Text strong>¥{s.median?.toFixed(0)}</Text></Text>
                        <Text type="secondary" style={{ fontSize: 12 }}>P25~P75 ¥{s.p25?.toFixed(0)}~{s.p75?.toFixed(0)}</Text>
                        {sideView === 'xianyu' && s.active_listings != null && (
                          <Tooltip title="同词在卖挂牌数，越多越红海">
                            <Text type="secondary" style={{ fontSize: 12 }}>在卖 <Text strong>{s.active_listings}</Text></Text>
                          </Tooltip>
                        )}
                      </Space>
                    );
                  })()
                }
              >
                <SideTable side={sideView === 'xianyu' ? analysis?.xianyu ?? null : analysis?.pdd ?? null} platform={sideView} />
                <Divider style={{ margin: '8px 0' }} />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  点击行展开查看该商品的各维度得分。判定：<Tag color="green">推荐</Tag><Tag color="orange">观察</Tag><Tag color="red">跳过</Tag>
                </Text>
              </Card>
            </>
          )}
        </Space>
      </Col>
    </Row>
  );
};

export default TenDimSelection;
