import React from 'react';
import { Card, Col, Row, Typography } from 'antd';

const { Title, Paragraph, Text } = Typography;

const Customer: React.FC = () => {
  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>
        客服中心
      </Title>
      <Paragraph type="secondary">
        左侧为会话列表，右侧为消息详情与回复区（占位布局）。
      </Paragraph>
      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col xs={24} md={8} lg={7}>
          <Card title="会话列表" style={{ minHeight: 420 }}>
            <Text type="secondary">
              各平台买家会话将按时间排序展示于此，支持未读筛选与账号切换。
            </Text>
          </Card>
        </Col>
        <Col xs={24} md={16} lg={17}>
          <Card title="消息详情" style={{ minHeight: 420 }}>
            <Text type="secondary">选择左侧会话查看详情</Text>
          </Card>
        </Col>
      </Row>
    </div>
  );
};

export default Customer;
