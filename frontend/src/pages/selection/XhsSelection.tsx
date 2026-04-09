import React, { useState, useEffect, useCallback } from 'react';
import {
  Typography, Table, Card, Tabs, Row, Col, Statistic, Tag, Space,
  Button, Progress, Modal, Descriptions, Badge, Empty,
} from 'antd';
import {
  FireOutlined, RiseOutlined, EyeOutlined, ReloadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';

const { Title, Text } = Typography;

interface TopicItem {
  id: string;
  topic_name: string;
  category: string;
  view_count: number;
  note_count: number;
  growth_rate_daily: number | null;
  captured_at: string;
}

interface KeywordItem {
  keyword: string;
  source: string;
  search_volume: number | null;
  growth_rate: number | null;
  has_supply: boolean;
}

interface RecommendItem {
  product: { id: string; title: string; price: number; category: string; image_urls: string[] | null };
  score: { total_score: number; decision: string; decision_label: string; dimensions: Record<string, { score: number; max: number; label: string }> };
}

const formatCount = (n: number): string => {
  if (n >= 100000000) return `${(n / 100000000).toFixed(1)}亿`;
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
  return String(n);
};

const xhsDecisionColors: Record<string, string> = {
  strong_recommend: 'green',
  worth_doing: 'blue',
  wait_and_see: 'orange',
  not_suitable: 'red',
};

const XhsSelection: React.FC = () => {
  const [topics, setTopics] = useState<TopicItem[]>([]);
  const [keywords, setKeywords] = useState<KeywordItem[]>([]);
  const [recommendations, setRecommendations] = useState<RecommendItem[]>([]);
  const [recTotal, setRecTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [detailVisible, setDetailVisible] = useState(false);
  const [detailItem, setDetailItem] = useState<RecommendItem | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [topicRes, kwRes, recRes] = await Promise.all([
        api.get('/xhs/trending/topics').catch(() => ({ data: { items: [] } })),
        api.get('/xhs/trending/keywords').catch(() => ({ data: { items: [] } })),
        api.get('/xhs/recommendations', { params: { page: 1, page_size: 20 } }).catch(() => ({ data: { items: [], total: 0 } })),
      ]);
      setTopics(topicRes.data.items || []);
      setKeywords(kwRes.data.items || []);
      setRecommendations(recRes.data.items || []);
      setRecTotal(recRes.data.total || 0);
    } catch { /* handled */ }
    setLoading(false);
  }, []);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const topicColumns: ColumnsType<TopicItem> = [
    { title: '话题', dataIndex: 'topic_name', render: (t: string) => <Text strong>#{t}</Text> },
    { title: '品类', dataIndex: 'category', width: 80, render: (c: string) => <Tag>{c}</Tag> },
    { title: '浏览量', dataIndex: 'view_count', width: 100, sorter: (a, b) => a.view_count - b.view_count, defaultSortOrder: 'descend', render: (v: number) => formatCount(v) },
    { title: '笔记数', dataIndex: 'note_count', width: 90, render: (v: number) => formatCount(v) },
    { title: '日增长', dataIndex: 'growth_rate_daily', width: 90, render: (v: number | null) => v != null ? <Text type={v > 10 ? 'success' : undefined}>{v > 0 ? '+' : ''}{v?.toFixed(1)}%</Text> : '-' },
  ];

  const kwColumns: ColumnsType<KeywordItem> = [
    { title: '关键词', dataIndex: 'keyword' },
    { title: '来源', dataIndex: 'source', width: 90, render: (s: string) => <Tag>{s === 'hot_search' ? '热搜' : s === 'comment_mining' ? '评论挖掘' : s}</Tag> },
    { title: '搜索量', dataIndex: 'search_volume', width: 90, render: (v: number | null) => v ? formatCount(v) : '-' },
    { title: '增长', dataIndex: 'growth_rate', width: 80, render: (v: number | null) => v != null ? <Text type="success">+{v?.toFixed(0)}%</Text> : '-' },
    { title: '有货源', dataIndex: 'has_supply', width: 70, render: (v: boolean) => v ? <Badge status="success" text="有" /> : <Badge status="default" text="无" /> },
  ];

  const recColumns: ColumnsType<RecommendItem> = [
    {
      title: '商品', dataIndex: ['product', 'title'], width: 240, ellipsis: true,
      render: (title: string, r: RecommendItem) => (
        <Space>
          {r.product.image_urls?.[0] && <img src={r.product.image_urls[0]} alt="" style={{ width: 36, height: 36, borderRadius: 4, objectFit: 'cover' }} />}
          <Text ellipsis style={{ maxWidth: 180 }}>{title}</Text>
        </Space>
      ),
    },
    { title: '采购价', dataIndex: ['product', 'price'], width: 80, render: (v: number) => `¥${v}` },
    {
      title: '评分', dataIndex: ['score', 'total_score'], width: 100, defaultSortOrder: 'descend',
      sorter: (a, b) => (a.score?.total_score || 0) - (b.score?.total_score || 0),
      render: (s: number) => {
        const color = s >= 80 ? '#52c41a' : s >= 60 ? '#1677ff' : s >= 40 ? '#faad14' : '#ff4d4f';
        return <Progress type="circle" percent={s} size={36} strokeColor={color} format={(p) => p} />;
      },
    },
    {
      title: '判定', dataIndex: ['score', 'decision'], width: 140,
      render: (d: string, r: RecommendItem) => <Tag color={xhsDecisionColors[d]}>{r.score?.decision_label}</Tag>,
    },
    {
      title: '操作', width: 120,
      render: (_: unknown, r: RecommendItem) => (
        <Space>
          <Button size="small" onClick={() => { setDetailItem(r); setDetailVisible(true); }}>详情</Button>
          <Button size="small" type="primary">出内容</Button>
        </Space>
      ),
    },
  ];

  return (
    <>
      <Title level={4}>小红书选品</Title>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={12} sm={6}><Card><Statistic title="飙升话题" value={topics.filter(t => (t.growth_rate_daily || 0) > 10).length} prefix={<FireOutlined />} valueStyle={{ color: '#ff4d4f' }} /></Card></Col>
        <Col xs={12} sm={6}><Card><Statistic title="追踪话题" value={topics.length} prefix={<EyeOutlined />} /></Card></Col>
        <Col xs={12} sm={6}><Card><Statistic title="飙升关键词" value={keywords.length} prefix={<RiseOutlined />} /></Card></Col>
        <Col xs={12} sm={6}><Card><Statistic title="推荐选品" value={recTotal} valueStyle={{ color: '#52c41a' }} /></Card></Col>
      </Row>

      <Card
        extra={<Button icon={<ReloadOutlined />} onClick={fetchAll} loading={loading}>刷新</Button>}
      >
        <Tabs items={[
          {
            key: 'recommend',
            label: '选品推荐',
            children: (
              <Table columns={recColumns} dataSource={recommendations} rowKey={(r) => r.product.id} pagination={{ pageSize: 10 }}
                locale={{ emptyText: <Empty description="启动爬虫后将自动生成小红书选品推荐" /> }} />
            ),
          },
          {
            key: 'topics',
            label: `热门话题 (${topics.length})`,
            children: <Table columns={topicColumns} dataSource={topics} rowKey="id" pagination={{ pageSize: 10 }}
              locale={{ emptyText: <Empty description="话题数据采集中..." /> }} />,
          },
          {
            key: 'keywords',
            label: `飙升关键词 (${keywords.length})`,
            children: <Table columns={kwColumns} dataSource={keywords} rowKey="keyword" pagination={{ pageSize: 15 }}
              locale={{ emptyText: <Empty description="关键词数据采集中..." /> }} />,
          },
        ]} />
      </Card>

      <Modal title="小红书五维度评分" open={detailVisible} onCancel={() => setDetailVisible(false)} footer={null} width={580}>
        {detailItem && (
          <>
            <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="商品" span={2}>{detailItem.product.title}</Descriptions.Item>
              <Descriptions.Item label="采购价">¥{detailItem.product.price}</Descriptions.Item>
              <Descriptions.Item label="综合评分"><Text strong style={{ fontSize: 18 }}>{detailItem.score?.total_score}</Text>/100</Descriptions.Item>
            </Descriptions>
            {detailItem.score?.dimensions && (
              <Card title="五维度评分" size="small">
                {Object.entries(detailItem.score.dimensions).map(([name, dim]) => (
                  <div key={name} style={{ display: 'flex', alignItems: 'center', marginBottom: 10 }}>
                    <Text style={{ width: 120 }}>{name}</Text>
                    <Progress percent={Math.round((dim.score / dim.max) * 100)} size="small" style={{ flex: 1, marginRight: 12 }} format={() => `${dim.score}/${dim.max}`} />
                    <Tag>{dim.label}</Tag>
                  </div>
                ))}
              </Card>
            )}
          </>
        )}
      </Modal>
    </>
  );
};

export default XhsSelection;
