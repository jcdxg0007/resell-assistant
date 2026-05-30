import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Typography, Table, Card, Input, InputNumber, Button, Space, Tag, Row, Col,
  App, Alert, Badge, Drawer, Tooltip, List, Popconfirm, Empty, Switch, Segmented,
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
    item_wants?: number | null;
  };
}

// ── PDD 侧 ────────────────────────────────────────────────────
interface PendingKw {
  keyword_id: string; text: string; category_name: string | null; pdd_mode: string;
  pdd_pending?: boolean; xianyu_pending?: boolean;
  pdd_eta_sec?: number | null; xianyu_eta_sec?: number | null;
}
interface CollectedKw {
  keyword_text: string; category_name: string | null; run_id: string | null;
  last_run_at: string | null;
  pdd?: { status: string; items_count: number; last_at: string | null } | null;
  xianyu?: { items_count: number; last_at: string | null } | null;
}
interface RiskItem { id: string; keyword_text: string; risk_signals: string[]; created_at: string | null; }
interface PddProduct { title?: string; price?: number | string; sales?: number; badges?: string[]; }

interface Console {
  stats: { total: number; items_total: number; success_rate: number | null; risk_blocked: number };
  target_count_min: number | null;
  target_count_max: number | null;
  auto_batch_enabled?: boolean;
  auto_next_at?: string | null;
  xianyu_auto_batch_enabled?: boolean;
  xianyu_auto_next_at?: string | null;
  pending: PendingKw[];
  collected: CollectedKw[];
  recent_risk: RiskItem[];
  worker: { online: boolean; devices?: string[] };
  paused?: boolean;
  queued?: number;
}

interface AutoConfig {
  auto_batch_enabled: boolean;
  auto_active_start_hour: number;
  auto_active_end_hour: number;
  auto_interval_min_minutes: number;
  auto_interval_max_minutes: number;
  auto_batch_count: number;
  xianyu_auto_batch_enabled: boolean;
  xianyu_auto_active_start_hour: number;
  xianyu_auto_active_end_hour: number;
  xianyu_auto_interval_min_minutes: number;
  xianyu_auto_interval_max_minutes: number;
  xianyu_auto_batch_count: number;
}

const PDD_STATUS_META: Record<string, { color: string; label: string }> = {
  ok: { color: 'success', label: '成功' },
  empty: { color: 'default', label: '空结果' },
  partial: { color: 'gold', label: '部分' },
  failed: { color: 'error', label: '失败' },
  risk_blocked: { color: 'volcano', label: '风控' },
  timeout: { color: 'orange', label: '超时' },
};
const fmtTime = (iso: string | null) => {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
};

const fmtHM = (iso: string | null | undefined) => {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' });
};

const fmtEta = (sec: number | null | undefined) => {
  if (sec == null) return '—';
  if (sec < 60) return '即将';
  if (sec < 3600) return `~${Math.round(sec / 60)}分`;
  return `~${(sec / 3600).toFixed(1)}时`;
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

  // PDD 控制台
  const [cons, setCons] = useState<Console | null>(null);
  const [consLoading, setConsLoading] = useState(false);

  // 商品量范围（可编辑）
  const [tcMin, setTcMin] = useState<number | null>(null);
  const [tcMax, setTcMax] = useState<number | null>(null);
  const [savingRange, setSavingRange] = useState(false);

  // 批量任务：平台选择（闲鱼/PDD/同时）+ 开始/暂停
  const [batchPlatform, setBatchPlatform] = useState<'both' | 'pdd' | 'xianyu'>('both');
  const [batchLoading, setBatchLoading] = useState(false);

  // 选中的已采集关键词 + 其商品
  const [selectedKw, setSelectedKw] = useState<string | null>(null);
  const [items, setItems] = useState<PddProduct[]>([]);
  const [itemsLoading, setItemsLoading] = useState(false);

  // 采集节奏控制窗口
  const [rhythmOpen, setRhythmOpen] = useState(false);

  // 全自动跑批配置
  const [auto, setAuto] = useState<AutoConfig | null>(null);
  const [savingAuto, setSavingAuto] = useState(false);

  const loadAuto = useCallback(async () => {
    try {
      const res = await api.get('/pdd-worker-config/');
      const c = res.data || {};
      setAuto({
        auto_batch_enabled: !!c.auto_batch_enabled,
        auto_active_start_hour: c.auto_active_start_hour ?? 9,
        auto_active_end_hour: c.auto_active_end_hour ?? 23,
        auto_interval_min_minutes: c.auto_interval_min_minutes ?? 40,
        auto_interval_max_minutes: c.auto_interval_max_minutes ?? 120,
        auto_batch_count: c.auto_batch_count ?? 3,
        xianyu_auto_batch_enabled: !!c.xianyu_auto_batch_enabled,
        xianyu_auto_active_start_hour: c.xianyu_auto_active_start_hour ?? 9,
        xianyu_auto_active_end_hour: c.xianyu_auto_active_end_hour ?? 23,
        xianyu_auto_interval_min_minutes: c.xianyu_auto_interval_min_minutes ?? 40,
        xianyu_auto_interval_max_minutes: c.xianyu_auto_interval_max_minutes ?? 120,
        xianyu_auto_batch_count: c.xianyu_auto_batch_count ?? 3,
      });
    } catch { /* 静默 */ }
  }, []);

  const saveAuto = useCallback(async (patch: Partial<AutoConfig>) => {
    setSavingAuto(true);
    try {
      await api.put('/pdd-worker-config/', { patch });
      setAuto((prev) => prev ? { ...prev, ...patch } : prev);
      message.success('已保存，下个唤醒周期生效');
    } catch (err) {
      const e = err as { response?: { data?: { detail?: string } } };
      message.error(e?.response?.data?.detail || '保存失败');
      await loadAuto();
    } finally {
      setSavingAuto(false);
    }
  }, [message, loadAuto]);

  // kw 给定时按关键词过滤（闲鱼商品落库时 category 存的就是搜索词），实现同词比价。
  // 改调 /xianyu/raw：比价页只展示采集到的原始挂牌，打分已挪到「十维度选品」页。
  const fetchXianyu = useCallback(async (p: number = 1, kw?: string | null) => {
    setXyLoading(true);
    try {
      const params: Record<string, unknown> = { page: p, page_size: 10 };
      if (kw) params.category = kw;
      const res = await api.get('/selection/xianyu/raw', { params });
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
  useEffect(() => { loadAuto(); }, [loadAuto]);
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

  // 待采集池里直接采一个词：按该词还缺哪个平台就派哪个（闲鱼失败不阻断 PDD）
  const dispatchPending = async (p: PendingKw) => {
    const kw = p.text;
    try {
      const jobs: Promise<unknown>[] = [];
      if (p.pdd_pending) jobs.push(searchPdd(kw));
      if (p.xianyu_pending) jobs.push(searchXianyu(kw).catch(() => undefined));
      if (jobs.length === 0) jobs.push(searchXianyu(kw));  // 兜底
      await Promise.all(jobs);
      message.success(`已派发「${kw}」，结果生成后自动刷新`);
      startAutoRefresh(kw);
    } catch (err) {
      const e = err as { response?: { status?: number } };
      message.error(e.response?.status === 503 ? 'PDD Worker 离线' : '派发失败');
    }
  };

  // 批量任务（按所选平台跑今日待采集池）
  const startBatch = async () => {
    setBatchLoading(true);
    try {
      const res = await api.post('/pdd-runs/batch/start', { platform: batchPlatform });
      const d = res.data;
      const parts: string[] = [];
      if (d.enqueued) parts.push(`PDD 排入 ${d.enqueued} 个词`);
      if (d.xianyu_scheduled) parts.push(`闲鱼 ${d.xianyu_scheduled} 个按 ~90s 错峰陆续跑`);
      message.success(parts.length ? `已开始：${parts.join('；')}` : '今日待采集池为空，无需开始');
      fetchConsole();
    } catch (err) {
      const e = err as { response?: { status?: number } };
      message.error(e.response?.status === 503 ? 'PDD Worker 离线，无法开始 PDD 任务' : '开始任务失败');
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

  // 闲鱼采集结果清空（硬删除，不可恢复）
  const clearXianyuCurrent = async () => {
    if (!selectedKw) return;
    try {
      const res = await api.delete('/selection/xianyu/products', { params: { category: selectedKw } });
      message.success(`已删除闲鱼「${selectedKw}」结果（${res.data.deleted} 条）`);
      fetchXianyu(1, selectedKw);
    } catch { message.error('清空失败'); }
  };

  const clearXianyuAll = async () => {
    try {
      const res = await api.delete('/selection/xianyu/products');
      message.success(`已删除闲鱼全部采集结果（${res.data.deleted} 条）`);
      fetchXianyu(1, selectedKw);
    } catch { message.error('清空失败'); }
  };

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
          <Text ellipsis style={{ maxWidth: 240 }}>{title}</Text>
        </Space>
      ),
    },
    { title: '价格', dataIndex: ['product', 'price'], width: 90, sorter: (a, b) => a.product.price - b.product.price, render: (v: number) => <Text>¥{v?.toFixed(2)}</Text> },
    {
      title: '想要', dataIndex: ['product', 'item_wants'], width: 80,
      sorter: (a, b) => (a.product.item_wants || 0) - (b.product.item_wants || 0), defaultSortOrder: 'descend',
      render: (v?: number | null) => <Text type="secondary">{v ?? 0}</Text>,
    },
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
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
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
          <Space size={12} wrap>
            <Tooltip title="选择「开始任务」批量跑哪个平台的待采集词：同时 = 两边各按各自待采集集合派">
              <Segmented
                size="small"
                value={batchPlatform}
                onChange={(v) => setBatchPlatform(v as 'both' | 'pdd' | 'xianyu')}
                options={[{ label: '同时', value: 'both' }, { label: '仅闲鱼', value: 'xianyu' }, { label: '仅PDD', value: 'pdd' }]}
              />
            </Tooltip>
            <Button size="small" type="primary" icon={<PlayCircleOutlined />} loading={batchLoading} onClick={startBatch}>开始任务</Button>
            {batchRunning && (
              <Button size="small" danger icon={<PauseCircleOutlined />} loading={batchLoading} onClick={pauseBatch}>暂停 PDD</Button>
            )}
            {(cons?.queued ?? 0) > 0 && <Text type="secondary" style={{ fontSize: 12 }}>PDD 队列 {cons?.queued}</Text>}
            {cons?.paused && <Tag color="orange">已暂停</Tag>}
          </Space>
        </Space>
      </Card>

      {/* 全自动采集：闲鱼 / PDD 各一套独立开关（beat 定时随机错峰派词，都走词库） */}
      <Row gutter={16}>
        <Col xs={24} md={12}>
          <Card
            title={<Space><Tag color="gold">闲鱼</Tag>全自动采集</Space>}
            size="small"
            extra={
              auto?.xianyu_auto_batch_enabled
                ? <Tag color="green">运行中{cons?.xianyu_auto_next_at ? ` · 下次约 ${fmtHM(cons.xianyu_auto_next_at)}` : ''}</Tag>
                : <Tag>已关闭</Tag>
            }
          >
            {auto ? (
              <Space size="large" wrap>
                <Space size={4}>
                  <Text type="secondary" style={{ fontSize: 12 }}>自动开关</Text>
                  <Switch
                    size="small" checked={auto.xianyu_auto_batch_enabled} loading={savingAuto}
                    onChange={(v) => saveAuto({ xianyu_auto_batch_enabled: v })}
                  />
                </Space>
                <Space size={4}>
                  <Text type="secondary" style={{ fontSize: 12 }}>活跃时段</Text>
                  <InputNumber
                    size="small" min={0} max={23} value={auto.xianyu_auto_active_start_hour} style={{ width: 56 }}
                    onChange={(v) => setAuto((p) => p ? { ...p, xianyu_auto_active_start_hour: v ?? 0 } : p)}
                    onBlur={() => saveAuto({ xianyu_auto_active_start_hour: auto.xianyu_auto_active_start_hour })}
                  />
                  <Text type="secondary">~</Text>
                  <InputNumber
                    size="small" min={0} max={23} value={auto.xianyu_auto_active_end_hour} style={{ width: 56 }}
                    onChange={(v) => setAuto((p) => p ? { ...p, xianyu_auto_active_end_hour: v ?? 0 } : p)}
                    onBlur={() => saveAuto({ xianyu_auto_active_end_hour: auto.xianyu_auto_active_end_hour })}
                    addonAfter="点"
                  />
                </Space>
                <Space size={4}>
                  <Tooltip title="两波之间的间隔在此区间内随机取，避免固定钟点被识别为机器">
                    <Text type="secondary" style={{ fontSize: 12 }}>随机间隔</Text>
                  </Tooltip>
                  <InputNumber
                    size="small" min={5} max={720} value={auto.xianyu_auto_interval_min_minutes} style={{ width: 64 }}
                    onChange={(v) => setAuto((p) => p ? { ...p, xianyu_auto_interval_min_minutes: v ?? 5 } : p)}
                    onBlur={() => saveAuto({ xianyu_auto_interval_min_minutes: auto.xianyu_auto_interval_min_minutes })}
                  />
                  <Text type="secondary">~</Text>
                  <InputNumber
                    size="small" min={5} max={1440} value={auto.xianyu_auto_interval_max_minutes} style={{ width: 64 }}
                    onChange={(v) => setAuto((p) => p ? { ...p, xianyu_auto_interval_max_minutes: v ?? 5 } : p)}
                    onBlur={() => saveAuto({ xianyu_auto_interval_max_minutes: auto.xianyu_auto_interval_max_minutes })}
                    addonAfter="分"
                  />
                </Space>
                <Space size={4}>
                  <Text type="secondary" style={{ fontSize: 12 }}>每波词数</Text>
                  <InputNumber
                    size="small" min={1} max={10} value={auto.xianyu_auto_batch_count} style={{ width: 56 }}
                    onChange={(v) => setAuto((p) => p ? { ...p, xianyu_auto_batch_count: v ?? 1 } : p)}
                    onBlur={() => saveAuto({ xianyu_auto_batch_count: auto.xianyu_auto_batch_count })}
                  />
                </Space>
              </Space>
            ) : <Text type="secondary">加载中…</Text>}
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card
            title={<Space><Tag color="red">PDD</Tag>全自动采集</Space>}
            size="small"
            extra={
              auto?.auto_batch_enabled
                ? <Tag color="green">运行中{cons?.auto_next_at ? ` · 下次约 ${fmtHM(cons.auto_next_at)}` : ''}</Tag>
                : <Tag>已关闭</Tag>
            }
          >
            {auto ? (
              <Space size="large" wrap>
                <Space size={4}>
                  <Text type="secondary" style={{ fontSize: 12 }}>自动开关</Text>
                  <Switch
                    size="small" checked={auto.auto_batch_enabled} loading={savingAuto}
                    onChange={(v) => saveAuto({ auto_batch_enabled: v })}
                  />
                </Space>
                <Space size={4}>
                  <Text type="secondary" style={{ fontSize: 12 }}>活跃时段</Text>
                  <InputNumber
                    size="small" min={0} max={23} value={auto.auto_active_start_hour} style={{ width: 56 }}
                    onChange={(v) => setAuto((p) => p ? { ...p, auto_active_start_hour: v ?? 0 } : p)}
                    onBlur={() => saveAuto({ auto_active_start_hour: auto.auto_active_start_hour })}
                  />
                  <Text type="secondary">~</Text>
                  <InputNumber
                    size="small" min={0} max={23} value={auto.auto_active_end_hour} style={{ width: 56 }}
                    onChange={(v) => setAuto((p) => p ? { ...p, auto_active_end_hour: v ?? 0 } : p)}
                    onBlur={() => saveAuto({ auto_active_end_hour: auto.auto_active_end_hour })}
                    addonAfter="点"
                  />
                </Space>
                <Space size={4}>
                  <Tooltip title="两波之间的间隔在此区间内随机取，避免固定钟点被识别为机器">
                    <Text type="secondary" style={{ fontSize: 12 }}>随机间隔</Text>
                  </Tooltip>
                  <InputNumber
                    size="small" min={5} max={720} value={auto.auto_interval_min_minutes} style={{ width: 64 }}
                    onChange={(v) => setAuto((p) => p ? { ...p, auto_interval_min_minutes: v ?? 5 } : p)}
                    onBlur={() => saveAuto({ auto_interval_min_minutes: auto.auto_interval_min_minutes })}
                  />
                  <Text type="secondary">~</Text>
                  <InputNumber
                    size="small" min={5} max={1440} value={auto.auto_interval_max_minutes} style={{ width: 64 }}
                    onChange={(v) => setAuto((p) => p ? { ...p, auto_interval_max_minutes: v ?? 5 } : p)}
                    onBlur={() => saveAuto({ auto_interval_max_minutes: auto.auto_interval_max_minutes })}
                    addonAfter="分"
                  />
                </Space>
                <Space size={4}>
                  <Text type="secondary" style={{ fontSize: 12 }}>每波词数</Text>
                  <InputNumber
                    size="small" min={1} max={10} value={auto.auto_batch_count} style={{ width: 56 }}
                    onChange={(v) => setAuto((p) => p ? { ...p, auto_batch_count: v ?? 1 } : p)}
                    onBlur={() => saveAuto({ auto_batch_count: auto.auto_batch_count })}
                  />
                </Space>
              </Space>
            ) : <Text type="secondary">加载中…</Text>}
          </Card>
        </Col>
      </Row>

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
                      <Button key="go" size="small" type="link" icon={<ThunderboltOutlined />} onClick={() => dispatchPending(p)}>采集</Button>,
                    ]}
                  >
                    <Space direction="vertical" size={2}>
                      <Space size={6}>
                        <Text>{p.text}</Text>
                        {p.category_name && <Tag>{p.category_name}</Tag>}
                        {p.xianyu_pending && <Tag color="gold">待闲鱼</Tag>}
                        {p.pdd_pending && <Tag color="red">待PDD</Tag>}
                      </Space>
                      {(p.pdd_pending && p.pdd_eta_sec != null) || (p.xianyu_pending && p.xianyu_eta_sec != null) ? (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          预估开始
                          {p.xianyu_pending && p.xianyu_eta_sec != null ? ` 闲鱼：${fmtEta(p.xianyu_eta_sec)}` : ''}
                          {p.pdd_pending && p.pdd_eta_sec != null ? ` PDD：${fmtEta(p.pdd_eta_sec)}` : ''}
                        </Text>
                      ) : null}
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
                    <Space direction="vertical" size={4}>
                      <Text strong={selectedKw === c.keyword_text}>{c.keyword_text}</Text>
                      <Space size={6} wrap>
                        {c.xianyu && (
                          <Tag color="gold">闲鱼 {fmtHM(c.xianyu.last_at)} · {c.xianyu.items_count}件</Tag>
                        )}
                        {c.pdd && (
                          <Tag color="red">
                            PDD {fmtHM(c.pdd.last_at)} · {c.pdd.items_count}件
                            {c.pdd.status !== 'ok' ? ` · ${PDD_STATUS_META[c.pdd.status]?.label || c.pdd.status}` : ''}
                          </Tag>
                        )}
                      </Space>
                    </Space>
                  </List.Item>
                )}
              />
            </div>
          </Card>
        </Col>
      </Row>

      {/* PDD采集结果 + 闲鱼采集结果 并排 */}
      <Row gutter={16}>
        <Col xs={24} xl={12}>
          <Card
            title={<Space><Tag color="red">PDD</Tag>采集结果{selectedKw && <Tag>{selectedKw}</Tag>}</Space>}
            styles={{ body: { padding: 12 } }}
            extra={
              <Space size={8} wrap>
                <Text type="secondary">成功率 <Text strong style={{ color: (stats?.success_rate ?? 0) >= 80 ? '#52c41a' : '#faad14' }}>{stats?.success_rate ?? 0}%</Text></Text>
                <Text type="secondary">商品 <Text strong>{stats?.items_total ?? 0}</Text></Text>
                <Text type="secondary">风控 <Text strong style={{ color: (stats?.risk_blocked ?? 0) > 0 ? '#cf1322' : undefined }}>{stats?.risk_blocked ?? 0}</Text></Text>
                <Popconfirm title={selectedKw ? `清空 PDD「${selectedKw}」今日结果？` : '请先选择一个已采集关键词'} onConfirm={clearCurrent} okText="清空" cancelText="取消" disabled={!selectedKw}>
                  <Button size="small" icon={<DeleteOutlined />} disabled={!selectedKw}>清空当前结果</Button>
                </Popconfirm>
                <Popconfirm title="清空 PDD 今日全部采集结果？" onConfirm={clearAll} okText="清空全部" okButtonProps={{ danger: true }} cancelText="取消">
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
        </Col>

        <Col xs={24} xl={12}>
          <Card
            title={<Space><Tag color="gold">闲鱼</Tag>采集结果{selectedKw ? <Tag>{selectedKw}</Tag> : <Text type="secondary" style={{ fontSize: 12 }}>（全部）</Text>}</Space>}
            styles={{ body: { padding: 12 } }}
            extra={
              <Space size={8} wrap>
                <Text type="secondary">共 {xyTotal}</Text>
                <Popconfirm title={selectedKw ? `清空闲鱼「${selectedKw}」结果？` : '请先选择一个已采集关键词'} onConfirm={clearXianyuCurrent} okText="清空" cancelText="取消" disabled={!selectedKw}>
                  <Button size="small" icon={<DeleteOutlined />} disabled={!selectedKw}>清空当前结果</Button>
                </Popconfirm>
                <Popconfirm title="清空闲鱼全部采集结果？此操作不可恢复" onConfirm={clearXianyuAll} okText="清空全部" okButtonProps={{ danger: true }} cancelText="取消">
                  <Button size="small" danger icon={<DeleteOutlined />}>清空全部结果</Button>
                </Popconfirm>
              </Space>
            }
          >
            <Table<ProductItem>
              size="small"
              columns={xianyuColumns}
              dataSource={xyData}
              rowKey={(r) => r.product.id}
              loading={xyLoading}
              pagination={{ current: xyPage, total: xyTotal, pageSize: 10, size: 'small', onChange: (p) => fetchXianyu(p, selectedKw), showSizeChanger: false }}
              locale={{ emptyText: selectedKw ? `闲鱼暂无「${selectedKw}」的采集数据（需先对该词跑过闲鱼搜索）` : '暂无闲鱼采集数据' }}
            />
          </Card>
        </Col>
      </Row>

      {/* 采集节奏控制窗口 */}
      <Drawer title="PDD 采集节奏控制" width={560} open={rhythmOpen} onClose={() => setRhythmOpen(false)} destroyOnHidden>
        <PddRhythmConfig embedded />
      </Drawer>
    </Space>
  );
};

export default MultiPlatformCompare;
