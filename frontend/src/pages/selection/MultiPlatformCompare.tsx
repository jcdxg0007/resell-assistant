import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Typography, Table, Card, Input, InputNumber, Button, Space, Tag, Row, Col,
  Progress, App, Alert, Modal, Descriptions, Badge, Drawer, Tooltip, List, Popconfirm, Empty, Switch,
} from 'antd';
import {
  ReloadOutlined, ControlOutlined, SyncOutlined, ThunderboltOutlined, DeleteOutlined,
  PlayCircleOutlined, PauseCircleOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';
import PddRhythmConfig from '../pdd/Config';

const { Title, Text } = Typography;

// ── 闲鱼侧：选品推荐 ──────────────────────────────────────────
interface ProductItem {
  product: {
    id: string; title: string; source_platform: string;
    price: number; category: string | null; image_urls: string[] | null;
  };
  score: {
    total_score: number; decision: string; decision_label: string;
    dimensions: Record<string, { score: number; max: number; label: string }>;
    scored_at: string | null;
  } | null;
}

const decisionColors: Record<string, string> = {
  strong_recommend: 'green', worth_try: 'blue', average: 'orange', skip: 'red',
};

// ── PDD 侧 ────────────────────────────────────────────────────
interface PendingKw { keyword_id: string; text: string; category_name: string | null; pdd_mode: string; }
interface CollectedKw { keyword_text: string; category_name: string | null; status: string; items_count: number; run_id: string; last_run_at: string | null; }
interface RiskItem { id: string; keyword_text: string; risk_signals: string[]; created_at: string | null; }
interface PddProduct { title?: string; price?: number | string; sales?: number; badges?: string[]; }

interface Console {
  stats: { total: number; items_total: number; success_rate: number | null; risk_blocked: number };
  target_count_min: number | null;
  target_count_max: number | null;
  pending: PendingKw[];
  collected: CollectedKw[];
  recent_risk: RiskItem[];
  worker: { online: boolean; devices?: string[] };
  paused?: boolean;
  queued?: number;
}

const PDD_STATUS_META: Record<string, { color: string; label: string }> = {
  ok: { color: 'success', label: '成功' },
  empty: { color: 'default', label: '空结果' },
  partial: { color: 'gold', label: '部分' },
  failed: { color: 'error', label: '失败' },
  risk_blocked: { color: 'volcano', label: '风控' },
  timeout: { color: 'orange', label: '超时' },
};
const pddStatusTag = (s: string) => {
  const m = PDD_STATUS_META[s] || { color: 'default', label: s };
  return <Tag color={m.color}>{m.label}</Tag>;
};

const fmtTime = (iso: string | null) => {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
};

const MultiPlatformCompare: React.FC = () => {
  const { message } = App.useApp();

  // 闲鱼推荐（底部比价用）
  const [xyLoading, setXyLoading] = useState(false);
  const [xyData, setXyData] = useState<ProductItem[]>([]);
  const [xyTotal, setXyTotal] = useState(0);
  const [xyPage, setXyPage] = useState(1);

  // 搜索
  const [keyword, setKeyword] = useState('');
  const [searchingXianyu, setSearchingXianyu] = useState(false);
  const [searchingPdd, setSearchingPdd] = useState(false);
  const [autoRefreshing, setAutoRefreshing] = useState(false);
  const pollRef = useRef<number | null>(null);

  // 详情
  const [detailVisible, setDetailVisible] = useState(false);
  const [detailItem, setDetailItem] = useState<ProductItem | null>(null);

  // PDD 控制台
  const [cons, setCons] = useState<Console | null>(null);
  const [consLoading, setConsLoading] = useState(false);

  // 商品量范围（可编辑）
  const [tcMin, setTcMin] = useState<number | null>(null);
  const [tcMax, setTcMax] = useState<number | null>(null);
  const [savingRange, setSavingRange] = useState(false);

  // 批量任务：同时跑开关（默认开）+ 开始/暂停
  const [bothPlatforms, setBothPlatforms] = useState(true);
  const [batchLoading, setBatchLoading] = useState(false);

  // 选中的已采集关键词 + 其商品
  const [selectedKw, setSelectedKw] = useState<string | null>(null);
  const [items, setItems] = useState<PddProduct[]>([]);
  const [itemsLoading, setItemsLoading] = useState(false);

  // 采集节奏控制窗口
  const [rhythmOpen, setRhythmOpen] = useState(false);

  // kw 给定时按关键词过滤（闲鱼商品落库时 category 存的就是搜索词），实现同词比价
  const fetchXianyu = useCallback(async (p: number = 1, kw?: string | null) => {
    setXyLoading(true);
    try {
      const params: Record<string, unknown> = { page: p, page_size: 10, min_score: 0 };
      if (kw) params.category = kw;
      const res = await api.get('/selection/xianyu/recommendations', { params });
      setXyData(res.data.items || []);
      setXyTotal(res.data.total || 0);
      setXyPage(p);
    } catch { /* ignore */ } finally { setXyLoading(false); }
  }, []);

  const fetchConsole = useCallback(async () => {
    setConsLoading(true);
    try {
      const res = await api.get('/pdd-runs/console');
      const c: Console = res.data;
      setCons(c);
      // 仅在用户没在编辑时同步范围输入框
      setTcMin((prev) => (prev === null ? c.target_count_min : prev));
      setTcMax((prev) => (prev === null ? c.target_count_max : prev));
    } catch { /* ignore */ } finally { setConsLoading(false); }
  }, []);

  // 选中一个词：同时加载 PDD 采到的商品 + 同词的闲鱼选品
  const loadItems = useCallback(async (kw: string) => {
    setItemsLoading(true);
    setSelectedKw(kw);
    fetchXianyu(1, kw);
    try {
      const res = await api.get('/pdd-runs/items', { params: { keyword: kw } });
      setItems(res.data.items || []);
    } catch { setItems([]); } finally { setItemsLoading(false); }
  }, [fetchXianyu]);

  useEffect(() => { fetchXianyu(); }, [fetchXianyu]);
  useEffect(() => { fetchConsole(); }, [fetchConsole]);
  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current); }, []);

  // 批量任务运行中（队列还有 + 未暂停）时，每 10s 刷新控制台看进度
  const batchRunning = !!cons && !cons.paused && (cons.queued ?? 0) > 0;
  useEffect(() => {
    if (!batchRunning) return;
    const id = window.setInterval(fetchConsole, 10000);
    return () => window.clearInterval(id);
  }, [batchRunning, fetchConsole]);

  const refreshAll = () => {
    fetchConsole();
    if (selectedKw) loadItems(selectedKw);
    else fetchXianyu(xyPage);
  };

  // 方案 A：提交后自动刷新（每 8s，约 80s）
  const startAutoRefresh = useCallback((watchKw?: string) => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    setAutoRefreshing(true);
    let n = 0;
    pollRef.current = window.setInterval(() => {
      n += 1;
      fetchConsole();
      if (watchKw) loadItems(watchKw);
      else fetchXianyu(1);
      if (n >= 10) {
        if (pollRef.current) window.clearInterval(pollRef.current);
        pollRef.current = null;
        setAutoRefreshing(false);
      }
    }, 8000);
  }, [fetchConsole, fetchXianyu, loadItems]);

  // 搜索动作
  const searchXianyu = async (kw?: string) => { await api.post('/products/search', { keyword: (kw ?? keyword).trim(), platform: 'xianyu' }); };
  const searchPdd = async (kw: string) => { await api.post('/pdd-runs/dispatch', { keyword: kw, mode: 'fast' }); };

  const handleXianyu = async () => {
    if (!keyword.trim()) { message.warning('请输入搜索关键词'); return; }
    setSearchingXianyu(true);
    try { await searchXianyu(); message.success('闲鱼搜索任务已提交'); }
    catch { message.error('闲鱼搜索任务提交失败'); }
    finally { setSearchingXianyu(false); }
  };

  const handlePdd = async () => {
    if (!keyword.trim()) { message.warning('请输入搜索关键词'); return; }
    setSearchingPdd(true);
    try {
      await searchPdd(keyword.trim());
      message.success('PDD 搜索已紧急派发，结果生成后自动刷新');
      startAutoRefresh(keyword.trim());
    } catch (err) {
      const e = err as { response?: { status?: number } };
      message.error(e.response?.status === 503 ? 'PDD Worker 离线，无法派发' : 'PDD 搜索派发失败');
    } finally { setSearchingPdd(false); }
  };

  const handleBoth = async () => {
    if (!keyword.trim()) { message.warning('请输入搜索关键词'); return; }
    setSearchingXianyu(true); setSearchingPdd(true);
    const kw = keyword.trim();
    const [xy, pdd] = await Promise.allSettled([searchXianyu(), searchPdd(kw)]);
    setSearchingXianyu(false); setSearchingPdd(false);
    const xyOk = xy.status === 'fulfilled';
    const pddOk = pdd.status === 'fulfilled';
    if (xyOk && pddOk) message.success('闲鱼 + PDD 任务已提交，结果生成后自动刷新');
    else if (xyOk) message.warning('闲鱼已提交；PDD 派发失败（worker 可能离线）');
    else if (pddOk) message.warning('PDD 已提交；闲鱼派发失败');
    else { message.error('两个平台都派发失败'); return; }
    startAutoRefresh(kw);
  };

  // 待采集池里直接采一个词（同时跑开启则闲鱼也跑）
  const dispatchPending = async (kw: string) => {
    try {
      await searchPdd(kw);
      if (bothPlatforms) { try { await searchXianyu(kw); } catch { /* 闲鱼失败不影响 PDD */ } }
      message.success(`已派发「${kw}」，结果生成后自动刷新`);
      startAutoRefresh(kw);
    } catch (err) {
      const e = err as { response?: { status?: number } };
      message.error(e.response?.status === 503 ? 'PDD Worker 离线' : '派发失败');
    }
  };

  // 批量任务
  const startBatch = async () => {
    setBatchLoading(true);
    try {
      const res = await api.post('/pdd-runs/batch/start', { both_platforms: bothPlatforms });
      const d = res.data;
      message.success(`已排入 ${d.enqueued} 个词${d.capped_by_quota ? '（受每日配额限制，剩余下次再跑）' : ''}，worker 按拟人节奏陆续采集`);
      fetchConsole();
    } catch (err) {
      const e = err as { response?: { status?: number } };
      message.error(e.response?.status === 503 ? 'PDD Worker 离线，无法开始' : '开始任务失败');
    } finally { setBatchLoading(false); }
  };

  const pauseBatch = async () => {
    setBatchLoading(true);
    try {
      const res = await api.post('/pdd-runs/batch/pause');
      message.success(`已暂停，清掉 ${res.data.purged} 个排队任务（在跑的不打断）`);
      fetchConsole();
    } catch { message.error('暂停失败'); }
    finally { setBatchLoading(false); }
  };

  const saveRange = async () => {
    if (tcMin == null || tcMax == null) { message.warning('请填写上下限'); return; }
    setSavingRange(true);
    try {
      await api.put('/pdd-worker-config/', { patch: { target_count_min: tcMin, target_count_max: tcMax } });
      message.success('商品量范围已保存，worker 下个心跳生效');
      fetchConsole();
    } catch (err) {
      const e = err as { response?: { data?: { detail?: string } } };
      message.error(e?.response?.data?.detail || '保存失败');
    } finally { setSavingRange(false); }
  };

  const clearCurrent = async () => {
    if (!selectedKw) return;
    try {
      await api.delete('/pdd-runs/today', { params: { keyword: selectedKw } });
      message.success(`已清空「${selectedKw}」今日结果`);
      setSelectedKw(null); setItems([]);
      fetchConsole(); fetchXianyu(1);
    } catch { message.error('清空失败'); }
  };

  const clearAll = async () => {
    try {
      await api.delete('/pdd-runs/today');
      message.success('已清空今日全部结果');
      setSelectedKw(null); setItems([]);
      fetchConsole(); fetchXianyu(1);
    } catch { message.error('清空失败'); }
  };

  const showDetail = (item: ProductItem) => { setDetailItem(item); setDetailVisible(true); };

  // ── 表格列 ──────────────────────────────────────────────
  const itemColumns: ColumnsType<PddProduct> = [
    { title: '商品', dataIndex: 'title', ellipsis: true, render: (t?: string) => <Text>{t || '—'}</Text> },
    { title: '价格', dataIndex: 'price', width: 90, render: (p?: number | string) => (p != null && p !== '') ? <Text>¥{p}</Text> : '—' },
    { title: '销量', dataIndex: 'sales', width: 90, render: (s?: number) => s ?? '—' },
    {
      title: '标签', dataIndex: 'badges', width: 200,
      render: (b?: string[]) => (b && b.length) ? <Space size={4} wrap>{b.slice(0, 3).map((x, i) => <Tag key={i}>{x}</Tag>)}</Space> : '—',
    },
  ];

  const xianyuColumns: ColumnsType<ProductItem> = [
    {
      title: '商品', dataIndex: ['product', 'title'], ellipsis: true,
      render: (title: string, r: ProductItem) => (
        <Space size={6}>
          {r.product.image_urls?.[0] && <img src={r.product.image_urls[0]} alt="" style={{ width: 32, height: 32, objectFit: 'cover', borderRadius: 4 }} />}
          <Text ellipsis style={{ maxWidth: 220 }}>{title}</Text>
        </Space>
      ),
    },
    { title: '采购价', dataIndex: ['product', 'price'], width: 90, sorter: (a, b) => a.product.price - b.product.price, render: (v: number) => <Text>¥{v?.toFixed(2)}</Text> },
    {
      title: '评分', dataIndex: ['score', 'total_score'], width: 80,
      sorter: (a, b) => (a.score?.total_score || 0) - (b.score?.total_score || 0), defaultSortOrder: 'descend',
      render: (score: number, r: ProductItem) => {
        if (!score) return <Text type="secondary">—</Text>;
        const color = score >= 80 ? '#52c41a' : score >= 60 ? '#1677ff' : score >= 40 ? '#faad14' : '#ff4d4f';
        return <Tooltip title={r.score?.decision_label}><Text strong style={{ color, cursor: 'pointer' }} onClick={() => showDetail(r)}>{score}</Text></Tooltip>;
      },
    },
    { title: '判定', dataIndex: ['score', 'decision'], width: 88, render: (d: string, r: ProductItem) => d ? <Tag color={decisionColors[d]}>{r.score?.decision_label}</Tag> : '—' },
  ];

  const worker = cons?.worker;
  const stats = cons?.stats;

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {/* 标题 + 工具栏 */}
      <Row justify="space-between" align="middle">
        <Col><Title level={4} style={{ margin: 0 }}>多平台比价</Title></Col>
        <Col>
          <Space>
            <Badge
              status={worker?.online ? 'success' : 'error'}
              text={worker?.online ? `PDD Worker 在线${worker.devices?.length ? `（${worker.devices.join(', ')}）` : ''}` : 'PDD Worker 离线'}
            />
            <Button icon={<ControlOutlined />} onClick={() => setRhythmOpen(true)}>采集节奏</Button>
            <Button icon={<ReloadOutlined />} onClick={refreshAll}>刷新</Button>
          </Space>
        </Col>
      </Row>

      {/* 搜索 */}
      <Card styles={{ body: { padding: 12 } }}>
        <Space.Compact style={{ width: '100%' }}>
          <Input
            placeholder="输入关键词，选择在闲鱼、拼多多或同时发起采集"
            value={keyword} onChange={(e) => setKeyword(e.target.value)} onPressEnter={handleBoth} allowClear
          />
          <Button onClick={handleXianyu} loading={searchingXianyu}>闲鱼搜索</Button>
          <Button onClick={handlePdd} loading={searchingPdd}>PDD搜索</Button>
          <Button type="primary" onClick={handleBoth} loading={searchingXianyu || searchingPdd}>同时搜</Button>
        </Space.Compact>
        {autoRefreshing && (
          <Space style={{ marginTop: 10 }} size={6}>
            <SyncOutlined spin style={{ color: '#1677ff' }} />
            <Text type="secondary">结果生成中，正在自动刷新…（约 1 分钟，也可手动点右上角刷新）</Text>
          </Space>
        )}
      </Card>

      {/* 今日搜索任务 + 商品量范围 + 批量开关 */}
      <Card
        title="今日搜索任务"
        size="small"
        loading={consLoading}
        extra={
          <Space size={12} wrap>
            <Tooltip title="开启后：批量任务和待采集池的「采集」按钮，每个词都同时跑闲鱼+PDD">
              <Space size={4}>
                <Text type="secondary" style={{ fontSize: 12 }}>关键词同时跑</Text>
                <Switch size="small" checked={bothPlatforms} onChange={setBothPlatforms} />
              </Space>
            </Tooltip>
            {(cons?.queued ?? 0) > 0 && <Text type="secondary" style={{ fontSize: 12 }}>队列 {cons?.queued}</Text>}
            {cons?.paused && <Tag color="orange">已暂停</Tag>}
            {batchRunning ? (
              <Button size="small" danger icon={<PauseCircleOutlined />} loading={batchLoading} onClick={pauseBatch}>暂停任务</Button>
            ) : (
              <Button size="small" type="primary" icon={<PlayCircleOutlined />} loading={batchLoading} onClick={startBatch}>开始任务</Button>
            )}
          </Space>
        }
      >
        <Space size="large" wrap>
          <Text>今日任务 <Text strong>{stats?.total ?? 0}</Text></Text>
          <Text type="secondary">待采集 <Text strong>{cons?.pending.length ?? 0}</Text></Text>
          <Space size={8}>
            <Text type="secondary">单词商品量</Text>
            <InputNumber size="small" min={1} max={100} value={tcMin} onChange={(v) => setTcMin(v)} style={{ width: 72 }} placeholder="下限" />
            <Text type="secondary">~</Text>
            <InputNumber size="small" min={1} max={100} value={tcMax} onChange={(v) => setTcMax(v)} style={{ width: 72 }} placeholder="上限" />
            <Button size="small" type="primary" loading={savingRange} onClick={saveRange}>保存</Button>
            <Tooltip title="每次采集一个关键词，目标商品数在此区间内随机取。worker 下个心跳生效。">
              <Text type="secondary" style={{ fontSize: 12 }}>采集量按此范围动态调整</Text>
            </Tooltip>
          </Space>
        </Space>
      </Card>

      {cons?.recent_risk && cons.recent_risk.length > 0 && (
        <Alert
          type="error" showIcon
          message={`PDD 今日有 ${cons.recent_risk.length} 次风控拦截，请关注`}
          description={
            <Space direction="vertical" size={2}>
              {cons.recent_risk.map((r) => (
                <Text key={r.id}>{fmtTime(r.created_at)} · <Text strong>{r.keyword_text}</Text>{r.risk_signals?.length ? ` · ${r.risk_signals.join(', ')}` : ''}</Text>
              ))}
            </Space>
          }
        />
      )}

      {/* 两个池子 */}
      <Row gutter={16}>
        <Col xs={24} md={12}>
          <Card title="今日待采集关键词" size="small" extra={<Text type="secondary">{cons?.pending.length ?? 0}</Text>}>
            <div style={{ maxHeight: 240, overflowY: 'auto' }}>
              <List
                size="small"
                dataSource={cons?.pending || []}
                locale={{ emptyText: '今日词库都已采集' }}
                renderItem={(p) => (
                  <List.Item
                    actions={[
                      <Button key="go" size="small" type="link" icon={<ThunderboltOutlined />} onClick={() => dispatchPending(p.text)}>采集</Button>,
                    ]}
                  >
                    <Space size={6}>
                      <Text>{p.text}</Text>
                      {p.category_name && <Tag>{p.category_name}</Tag>}
                    </Space>
                  </List.Item>
                )}
              />
            </div>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title="今日已采集关键词" size="small" extra={<Text type="secondary">{cons?.collected.length ?? 0}</Text>}>
            <div style={{ maxHeight: 240, overflowY: 'auto' }}>
              <List
                size="small"
                dataSource={cons?.collected || []}
                locale={{ emptyText: '今日还没有采集记录' }}
                renderItem={(c) => (
                  <List.Item
                    onClick={() => loadItems(c.keyword_text)}
                    style={{ cursor: 'pointer', background: selectedKw === c.keyword_text ? '#e6f4ff' : undefined, paddingInline: 8, borderRadius: 4 }}
                  >
                    <Space size={6}>
                      <Text strong={selectedKw === c.keyword_text}>{c.keyword_text}</Text>
                      {pddStatusTag(c.status)}
                      <Text type="secondary" style={{ fontSize: 12 }}>{c.items_count} 件</Text>
                    </Space>
                  </List.Item>
                )}
              />
            </div>
          </Card>
        </Col>
      </Row>

      {/* 采集结果 */}
      <Card
        title={<Space>采集结果{selectedKw && <Tag color="red">{selectedKw}</Tag>}</Space>}
        styles={{ body: { padding: 12 } }}
        extra={
          <Space size="large" wrap>
            <Text type="secondary">成功率 <Text strong style={{ color: (stats?.success_rate ?? 0) >= 80 ? '#52c41a' : '#faad14' }}>{stats?.success_rate ?? 0}%</Text></Text>
            <Text type="secondary">抓到商品 <Text strong>{stats?.items_total ?? 0}</Text></Text>
            <Text type="secondary">风控命中 <Text strong style={{ color: (stats?.risk_blocked ?? 0) > 0 ? '#cf1322' : undefined }}>{stats?.risk_blocked ?? 0}</Text></Text>
            <Popconfirm title={selectedKw ? `清空「${selectedKw}」今日结果？` : '请先选择一个已采集关键词'} onConfirm={clearCurrent} okText="清空" cancelText="取消" disabled={!selectedKw}>
              <Button size="small" icon={<DeleteOutlined />} disabled={!selectedKw}>清空当前结果</Button>
            </Popconfirm>
            <Popconfirm title="清空今日全部采集结果？此操作不可恢复" onConfirm={clearAll} okText="清空全部" okButtonProps={{ danger: true }} cancelText="取消">
              <Button size="small" danger icon={<DeleteOutlined />}>清空全部结果</Button>
            </Popconfirm>
          </Space>
        }
      >
        {selectedKw ? (
          <Table<PddProduct>
            size="small"
            rowKey={(_, i) => String(i)}
            loading={itemsLoading}
            columns={itemColumns}
            dataSource={items}
            pagination={{ pageSize: 20, size: 'small', showSizeChanger: false }}
            locale={{ emptyText: '该关键词本次未采到商品' }}
          />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="点上方「今日已采集关键词」查看采到的商品" />
        )}
      </Card>

      {/* 闲鱼选品结果（按选中词同词比价；未选词时显示全局推荐）*/}
      <Card
        title={<Space><Tag color="gold">闲鱼</Tag>选品结果{selectedKw ? <Tag>{selectedKw}</Tag> : <Text type="secondary" style={{ fontSize: 12 }}>（全局推荐）</Text>}</Space>}
        extra={<Text type="secondary">共 {xyTotal}</Text>}
        styles={{ body: { padding: 12 } }}
      >
        <Table<ProductItem>
          size="small"
          columns={xianyuColumns}
          dataSource={xyData}
          rowKey={(r) => r.product.id}
          loading={xyLoading}
          pagination={{ current: xyPage, total: xyTotal, pageSize: 10, size: 'small', onChange: (p) => fetchXianyu(p, selectedKw), showSizeChanger: false }}
          locale={{ emptyText: selectedKw ? `闲鱼暂无「${selectedKw}」的选品数据（需先对该词跑过闲鱼搜索）` : '暂无闲鱼选品数据' }}
        />
      </Card>

      {/* 采集节奏控制窗口 */}
      <Drawer title="PDD 采集节奏控制" width={560} open={rhythmOpen} onClose={() => setRhythmOpen(false)} destroyOnHidden>
        <PddRhythmConfig embedded />
      </Drawer>

      {/* 闲鱼评分详情 */}
      <Modal title="商品评分详情" open={detailVisible} onCancel={() => setDetailVisible(false)} footer={null} width={640}>
        {detailItem && (
          <>
            <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="商品名称" span={2}>{detailItem.product.title}</Descriptions.Item>
              <Descriptions.Item label="采购价">¥{detailItem.product.price}</Descriptions.Item>
              <Descriptions.Item label="综合评分"><Text strong style={{ fontSize: 18 }}>{detailItem.score?.total_score || '—'}</Text>/100</Descriptions.Item>
            </Descriptions>
            {detailItem.score?.dimensions && (
              <Card title="十维度评分" size="small">
                {Object.entries(detailItem.score.dimensions).map(([name, dim]) => (
                  <div key={name} style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
                    <Text style={{ width: 140 }}>{name}</Text>
                    <Progress percent={Math.round((dim.score / dim.max) * 100)} size="small" style={{ flex: 1, marginRight: 12 }} format={() => `${dim.score}/${dim.max}`} />
                    <Tag>{dim.label}</Tag>
                  </div>
                ))}
              </Card>
            )}
          </>
        )}
      </Modal>
    </Space>
  );
};

export default MultiPlatformCompare;
