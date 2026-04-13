import React, { useState, useEffect, useCallback } from 'react';
import {
  Typography, Table, Card, Input, Button, Space, Tag, Row, Col,
  Statistic, Progress, Descriptions, App, Alert, Modal,
} from 'antd';
import { SearchOutlined, ReloadOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';

const { Title, Text } = Typography;

interface ProductItem {
  product: {
    id: string;
    title: string;
    source_platform: string;
    price: number;
    category: string | null;
    image_urls: string[] | null;
  };
  score: {
    total_score: number;
    decision: string;
    decision_label: string;
    dimensions: Record<string, { score: number; max: number; label: string }>;
    scored_at: string | null;
  } | null;
}

const decisionColors: Record<string, string> = {
  strong_recommend: 'green',
  worth_try: 'blue',
  average: 'orange',
  skip: 'red',
};

const XianyuSelection: React.FC = () => {
  const { modal, message } = App.useApp();
  const [loading, setLoading] = useState(false);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchResult, setSearchResult] = useState<'success' | 'error' | null>(null);
  const [searchKeyword, setSearchKeyword] = useState('');
  const [data, setData] = useState<ProductItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [detailVisible, setDetailVisible] = useState(false);
  const [detailItem, setDetailItem] = useState<ProductItem | null>(null);

  const fetchRecommendations = useCallback(async (p: number = 1) => {
    setLoading(true);
    try {
      const res = await api.get('/selection/xianyu/recommendations', {
        params: { page: p, page_size: 20, min_score: 0 },
      });
      setData(res.data.items || []);
      setTotal(res.data.total || 0);
      setPage(p);
    } catch (err: any) {
      if (err.response?.status !== 401) {
        message.error('加载失败');
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRecommendations();
  }, [fetchRecommendations]);

  const handleSearch = async () => {
    if (!keyword.trim()) {
      message.warning('请输入搜索关键词');
      return;
    }
    setSearchLoading(true);
    setSearchResult(null);
    try {
      await api.post('/products/search', { keyword: keyword.trim(), platform: 'xianyu' });
      setSearchKeyword(keyword.trim());
      setSearchResult('success');
      message.success('搜索任务已提交');
    } catch {
      setSearchResult('error');
      message.error('搜索任务提交失败');
    } finally {
      setSearchLoading(false);
    }
  };

  const showDetail = (item: ProductItem) => {
    setDetailItem(item);
    setDetailVisible(true);
  };

  const columns: ColumnsType<ProductItem> = [
    {
      title: '商品',
      dataIndex: ['product', 'title'],
      width: 280,
      ellipsis: true,
      render: (title: string, record: ProductItem) => (
        <Space>
          {record.product.image_urls?.[0] && (
            <img src={record.product.image_urls[0]} alt="" style={{ width: 40, height: 40, objectFit: 'cover', borderRadius: 4 }} />
          )}
          <Text ellipsis style={{ maxWidth: 200 }}>{title}</Text>
        </Space>
      ),
    },
    {
      title: '来源',
      dataIndex: ['product', 'source_platform'],
      width: 80,
      render: (p: string) => <Tag>{p === 'pinduoduo' ? '拼多多' : p === 'taobao' ? '淘宝' : p}</Tag>,
    },
    {
      title: '采购价',
      dataIndex: ['product', 'price'],
      width: 90,
      sorter: (a, b) => a.product.price - b.product.price,
      render: (v: number) => <Text>¥{v.toFixed(2)}</Text>,
    },
    {
      title: '综合评分',
      dataIndex: ['score', 'total_score'],
      width: 120,
      sorter: (a, b) => (a.score?.total_score || 0) - (b.score?.total_score || 0),
      defaultSortOrder: 'descend',
      render: (score: number) => {
        if (!score) return <Text type="secondary">未评分</Text>;
        const color = score >= 80 ? '#52c41a' : score >= 60 ? '#1677ff' : score >= 40 ? '#faad14' : '#ff4d4f';
        return (
          <Space>
            <Progress type="circle" percent={score} size={36} strokeColor={color} format={(p) => p} />
          </Space>
        );
      },
    },
    {
      title: '判定',
      dataIndex: ['score', 'decision'],
      width: 100,
      render: (decision: string, record: ProductItem) => {
        if (!decision) return '-';
        return <Tag color={decisionColors[decision]}>{record.score?.decision_label}</Tag>;
      },
    },
    {
      title: '品类',
      dataIndex: ['product', 'category'],
      width: 100,
      render: (c: string) => c || '-',
    },
    {
      title: '操作',
      width: 150,
      render: (_: unknown, record: ProductItem) => (
        <Space>
          <Button size="small" onClick={() => showDetail(record)}>详情</Button>
          <Button size="small" type="primary">发布</Button>
        </Space>
      ),
    },
  ];

  return (
    <>
      <Title level={4}>闲鱼比价选品</Title>

      <Card style={{ marginBottom: 16 }}>
        <Row gutter={16} align="middle">
          <Col flex="auto">
            <Input.Search
              placeholder="搜索商品关键词或粘贴链接"
              enterButton={<><SearchOutlined /> 搜索</>}
              size="large"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onSearch={handleSearch}
              loading={searchLoading}
            />
          </Col>
          <Col>
            <Button icon={<ReloadOutlined />} onClick={() => fetchRecommendations(1)}>
              刷新数据
            </Button>
          </Col>
        </Row>
      </Card>

      {searchResult === 'success' && (
        <Alert
          message={`搜索任务已提交：「${searchKeyword}」`}
          description="爬虫正在后台抓取闲鱼数据，完成后刷新页面即可看到新商品"
          type="success"
          showIcon
          closable
          onClose={() => setSearchResult(null)}
          style={{ marginBottom: 16 }}
          action={
            <Button size="small" onClick={() => fetchRecommendations(1)}>
              刷新数据
            </Button>
          }
        />
      )}
      {searchResult === 'error' && (
        <Alert
          message="搜索任务提交失败"
          description="请检查网络连接或稍后重试"
          type="error"
          showIcon
          closable
          onClose={() => setSearchResult(null)}
          style={{ marginBottom: 16 }}
        />
      )}

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={12} sm={6}>
          <Card><Statistic title="已追踪商品" value={total} /></Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card><Statistic title="强烈推荐" value={data.filter(d => d.score?.decision === 'strong_recommend').length} valueStyle={{ color: '#52c41a' }} /></Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card><Statistic title="值得尝试" value={data.filter(d => d.score?.decision === 'worth_try').length} valueStyle={{ color: '#1677ff' }} /></Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card><Statistic title="今日新发现" value={0} /></Card>
        </Col>
      </Row>

      <Card>
        <Table
          columns={columns}
          dataSource={data}
          rowKey={(r) => r.product.id}
          loading={loading}
          pagination={{
            current: page,
            total,
            pageSize: 20,
            onChange: (p) => fetchRecommendations(p),
            showTotal: (t) => `共 ${t} 个商品`,
          }}
          scroll={{ x: 900 }}
          locale={{ emptyText: '暂无选品数据，启动选品引擎后将自动发现商品' }}
        />
      </Card>

      <Modal
        title="商品评分详情"
        open={detailVisible}
        onCancel={() => setDetailVisible(false)}
        footer={null}
        width={640}
      >
        {detailItem && (
          <>
            <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="商品名称" span={2}>{detailItem.product.title}</Descriptions.Item>
              <Descriptions.Item label="采购价">¥{detailItem.product.price}</Descriptions.Item>
              <Descriptions.Item label="综合评分">
                <Text strong style={{ fontSize: 18 }}>{detailItem.score?.total_score || '-'}</Text>/100
              </Descriptions.Item>
            </Descriptions>
            {detailItem.score?.dimensions && (
              <Card title="十维度评分" size="small">
                {Object.entries(detailItem.score.dimensions).map(([name, dim]) => (
                  <div key={name} style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
                    <Text style={{ width: 140 }}>{name}</Text>
                    <Progress
                      percent={Math.round((dim.score / dim.max) * 100)}
                      size="small"
                      style={{ flex: 1, marginRight: 12 }}
                      format={() => `${dim.score}/${dim.max}`}
                    />
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

export default XianyuSelection;
