import React, { useState, useEffect, useCallback } from 'react';
import { Card, Table, Typography, Button, Tag, message } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';

const { Title, Paragraph, Text } = Typography;

interface VirtualItem {
  id: string;
  title: string;
  price: number;
  category: string | null;
}

const VirtualSelection: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<VirtualItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);

  const fetchData = useCallback(async (p = 1) => {
    setLoading(true);
    try {
      const res = await api.get('/selection/virtual/recommendations', { params: { page: p, page_size: 20 } });
      setData(res.data.items || []);
      setTotal(res.data.total || 0);
      setPage(p);
    } catch { /* */ }
    setLoading(false);
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  const columns: ColumnsType<VirtualItem> = [
    { title: '商品名称', dataIndex: 'title', ellipsis: true },
    {
      title: '价格', dataIndex: 'price', width: 100,
      render: (v: number) => <Text>¥{v.toFixed(2)}</Text>,
    },
    {
      title: '品类', dataIndex: 'category', width: 120,
      render: (c: string) => c ? <Tag>{c}</Tag> : '-',
    },
  ];

  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>虚拟商品选品</Title>
      <Paragraph type="secondary">虚拟品类（资料、会员、服务等）的商品列表</Paragraph>

      <Card
        title="虚拟选品列表"
        style={{ marginTop: 16 }}
        extra={<Button icon={<ReloadOutlined />} onClick={() => fetchData(1)}>刷新</Button>}
      >
        <Table
          columns={columns}
          dataSource={data}
          rowKey="id"
          loading={loading}
          pagination={{ current: page, total, pageSize: 20, onChange: (p) => fetchData(p), showTotal: (t) => `共 ${t} 个` }}
          locale={{ emptyText: '暂无虚拟商品数据' }}
        />
      </Card>
    </div>
  );
};

export default VirtualSelection;
