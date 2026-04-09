import React from 'react';
import { Card, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

const { Title, Paragraph } = Typography;

interface VirtualRow {
  key: string;
}

const columns: ColumnsType<VirtualRow> = [
  { title: '品类', dataIndex: 'category', key: 'category' },
  { title: '商品名称', dataIndex: 'name', key: 'name' },
  { title: '搜索热度', dataIndex: 'searchHeat', key: 'searchHeat' },
  { title: '付费意愿', dataIndex: 'willingness', key: 'willingness' },
  { title: '竞品数量', dataIndex: 'competitors', key: 'competitors' },
  { title: '建议售价', dataIndex: 'suggestedPrice', key: 'suggestedPrice' },
  { title: '建议平台', dataIndex: 'platform', key: 'platform' },
  { title: '操作', key: 'action', width: 100 },
];

const VirtualSelection: React.FC = () => {
  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>
        虚拟商品选品
      </Title>
      <Paragraph type="secondary">
        虚拟品类（资料、会员、服务等）的热度与竞品情况将汇总在下表中。
      </Paragraph>
      <Card title="虚拟选品列表" style={{ marginTop: 16 }}>
        <Table<VirtualRow>
          rowKey="key"
          columns={columns}
          dataSource={[]}
          pagination={false}
          locale={{ emptyText: '暂无虚拟商品候选，接入数据源后将自动填充' }}
        />
      </Card>
    </div>
  );
};

export default VirtualSelection;
