import React from 'react';
import { Button, Card, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

const { Title, Paragraph } = Typography;

interface AccountRow {
  key: string;
}

const columns: ColumnsType<AccountRow> = [
  { title: '平台', dataIndex: 'platform', key: 'platform', width: 100 },
  { title: '账号名', dataIndex: 'name', key: 'name' },
  { title: '身份组', dataIndex: 'group', key: 'group' },
  { title: '品类定位', dataIndex: 'category', key: 'category' },
  { title: '生命周期', dataIndex: 'lifecycle', key: 'lifecycle' },
  { title: '今日已发', dataIndex: 'todayPosts', key: 'todayPosts', width: 90 },
  { title: '健康度', dataIndex: 'health', key: 'health', width: 90 },
  { title: '代理IP', dataIndex: 'proxy', key: 'proxy', ellipsis: true },
  { title: '状态', dataIndex: 'status', key: 'status', width: 90 },
  { title: '操作', key: 'action', width: 120 },
];

const Accounts: React.FC = () => {
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Title level={3} style={{ marginBottom: 4 }}>
          账号管理
        </Title>
        <Paragraph type="secondary">
          多平台账号矩阵、发信额度与健康度将在此维护，支持绑定代理与品类策略。
        </Paragraph>
      </div>
      <Card>
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <div>
            <Button type="primary">添加账号</Button>
          </div>
          <Table<AccountRow>
            rowKey="key"
            columns={columns}
            dataSource={[]}
            pagination={false}
            locale={{ emptyText: '暂无账号，请点击「添加账号」录入' }}
          />
        </Space>
      </Card>
    </Space>
  );
};

export default Accounts;
