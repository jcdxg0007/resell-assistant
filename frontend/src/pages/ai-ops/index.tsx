import React from 'react';
import { Card, Table, Tabs, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

const { Title, Paragraph } = Typography;

interface SuggestionRow {
  key: string;
}

interface CheckRow {
  key: string;
}

const suggestionColumns: ColumnsType<SuggestionRow> = [
  { title: '类型', dataIndex: 'type', key: 'type' },
  { title: '建议内容', dataIndex: 'content', key: 'content', ellipsis: true },
  { title: '优先级', dataIndex: 'priority', key: 'priority', width: 90 },
  { title: '状态', dataIndex: 'status', key: 'status', width: 90 },
  { title: '创建时间', dataIndex: 'createdAt', key: 'createdAt', width: 160 },
  { title: '操作(执行/忽略)', key: 'action', width: 140 },
];

const checkColumns: ColumnsType<CheckRow> = [
  { title: '检查项', dataIndex: 'item', key: 'item' },
  { title: '状态', dataIndex: 'status', key: 'status', width: 100 },
  { title: '详情', dataIndex: 'detail', key: 'detail', ellipsis: true },
  { title: '检查时间', dataIndex: 'checkedAt', key: 'checkedAt', width: 160 },
];

const AiOps: React.FC = () => {
  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>
        AI运营中枢
      </Title>
      <Paragraph type="secondary">
        日报摘要、可执行建议与每日自检任务将集中在此，便于一键跟进。
      </Paragraph>
      <Card style={{ marginTop: 16 }}>
        <Tabs
          items={[
            {
              key: 'daily',
              label: '运营日报',
              children: (
                <Card type="inner" title="今日运营摘要">
                  <Typography.Text type="secondary">
                    关键指标、异常账号与待办事项将在此生成；接入数据后展示 AI 生成的日报正文与图表占位。
                  </Typography.Text>
                </Card>
              ),
            },
            {
              key: 'suggestions',
              label: 'AI建议',
              children: (
                <Table<SuggestionRow>
                  rowKey="key"
                  columns={suggestionColumns}
                  dataSource={[]}
                  pagination={false}
                  locale={{ emptyText: '暂无 AI 建议，定时任务跑批后将写入' }}
                />
              ),
            },
            {
              key: 'selfcheck',
              label: '每日自检',
              children: (
                <Table<CheckRow>
                  rowKey="key"
                  columns={checkColumns}
                  dataSource={[]}
                  pagination={false}
                  locale={{ emptyText: '暂无自检记录' }}
                />
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
};

export default AiOps;
