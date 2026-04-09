import React from 'react';
import { Card, Table, Tabs, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

const { Title, Paragraph } = Typography;

interface DraftRow {
  key: string;
}

interface NoteRow {
  key: string;
}

interface ShopRow {
  key: string;
}

const draftColumns: ColumnsType<DraftRow> = [
  { title: '标题', dataIndex: 'title', key: 'title' },
  { title: '笔记类型', dataIndex: 'type', key: 'type' },
  { title: '关联商品', dataIndex: 'product', key: 'product' },
  { title: '创建时间', dataIndex: 'createdAt', key: 'createdAt' },
  { title: '操作(编辑/发布)', key: 'action', width: 140 },
];

const noteColumns: ColumnsType<NoteRow> = [
  { title: '标题', dataIndex: 'title', key: 'title' },
  { title: '点赞', dataIndex: 'likes', key: 'likes' },
  { title: '收藏', dataIndex: 'favorites', key: 'favorites' },
  { title: '评论', dataIndex: 'comments', key: 'comments' },
  { title: '互动率', dataIndex: 'engagement', key: 'engagement' },
  { title: '发布时间', dataIndex: 'publishedAt', key: 'publishedAt' },
  { title: '状态', dataIndex: 'status', key: 'status' },
];

const shopColumns: ColumnsType<ShopRow> = [
  { title: '商品名称', dataIndex: 'name', key: 'name' },
  { title: '售价', dataIndex: 'price', key: 'price' },
  { title: '库存', dataIndex: 'stock', key: 'stock' },
  { title: '状态', dataIndex: 'status', key: 'status' },
  { title: '操作', key: 'action', width: 100 },
];

const XhsWorkbench: React.FC = () => {
  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>
        小红书工作台
      </Title>
      <Paragraph type="secondary">
        草稿、已发笔记与店铺商品分栏展示，便于内容生产与货品联动。
      </Paragraph>
      <Card style={{ marginTop: 16 }}>
        <Tabs
          items={[
            {
              key: 'draft',
              label: '内容草稿',
              children: (
                <Table<DraftRow>
                  rowKey="key"
                  columns={draftColumns}
                  dataSource={[]}
                  pagination={false}
                  locale={{ emptyText: '暂无内容草稿' }}
                />
              ),
            },
            {
              key: 'notes',
              label: '已发布笔记',
              children: (
                <Table<NoteRow>
                  rowKey="key"
                  columns={noteColumns}
                  dataSource={[]}
                  pagination={false}
                  locale={{ emptyText: '暂无已发布笔记' }}
                />
              ),
            },
            {
              key: 'shop',
              label: '店铺商品',
              children: (
                <Table<ShopRow>
                  rowKey="key"
                  columns={shopColumns}
                  dataSource={[]}
                  pagination={false}
                  locale={{ emptyText: '暂无店铺商品数据' }}
                />
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
};

export default XhsWorkbench;
