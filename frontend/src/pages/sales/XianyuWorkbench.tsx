import React from 'react';
import { Card, Table, Tabs, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

const { Title, Paragraph } = Typography;

interface PendingRow {
  key: string;
}

interface PublishedRow {
  key: string;
}

interface OfflineRow {
  key: string;
}

const pendingColumns: ColumnsType<PendingRow> = [
  { title: '商品名称', dataIndex: 'name', key: 'name' },
  { title: '建议售价', dataIndex: 'suggestedPrice', key: 'suggestedPrice' },
  { title: '采购价', dataIndex: 'purchasePrice', key: 'purchasePrice' },
  { title: '预估利润', dataIndex: 'estimatedProfit', key: 'estimatedProfit' },
  { title: '状态', dataIndex: 'status', key: 'status' },
  { title: '操作(预览/发布)', key: 'action', width: 160 },
];

const publishedColumns: ColumnsType<PublishedRow> = [
  { title: '商品名称', dataIndex: 'name', key: 'name' },
  { title: '售价', dataIndex: 'price', key: 'price' },
  { title: '曝光', dataIndex: 'exposure', key: 'exposure' },
  { title: '想要数', dataIndex: 'wants', key: 'wants' },
  { title: '聊天数', dataIndex: 'chats', key: 'chats' },
  { title: '发布时间', dataIndex: 'publishedAt', key: 'publishedAt' },
  { title: '操作(擦亮/调价/下架)', key: 'action', width: 200 },
];

const offlineColumns: ColumnsType<OfflineRow> = [
  { title: '商品名称', dataIndex: 'name', key: 'name' },
  { title: '原售价', dataIndex: 'price', key: 'price' },
  { title: '下架时间', dataIndex: 'offlineAt', key: 'offlineAt' },
  { title: '下架原因', dataIndex: 'reason', key: 'reason' },
  { title: '操作', key: 'action', width: 120 },
];

const XianyuWorkbench: React.FC = () => {
  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>
        闲鱼工作台
      </Title>
      <Paragraph type="secondary">
        待发布草稿、在架商品与已下架商品分区管理，后续可对接擦亮与批量调价。
      </Paragraph>
      <Card style={{ marginTop: 16 }}>
        <Tabs
          items={[
            {
              key: 'pending',
              label: '待发布',
              children: (
                <Table<PendingRow>
                  rowKey="key"
                  columns={pendingColumns}
                  dataSource={[]}
                  pagination={false}
                  locale={{ emptyText: '暂无待发布商品' }}
                />
              ),
            },
            {
              key: 'published',
              label: '已发布',
              children: (
                <Table<PublishedRow>
                  rowKey="key"
                  columns={publishedColumns}
                  dataSource={[]}
                  pagination={false}
                  locale={{ emptyText: '暂无已发布商品' }}
                />
              ),
            },
            {
              key: 'offline',
              label: '已下架',
              children: (
                <Table<OfflineRow>
                  rowKey="key"
                  columns={offlineColumns}
                  dataSource={[]}
                  pagination={false}
                  locale={{ emptyText: '暂无已下架记录' }}
                />
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
};

export default XianyuWorkbench;
