import React from 'react';
import { Card, Form, Input, Tabs, Typography } from 'antd';

const { Title, Paragraph } = Typography;

const Settings: React.FC = () => {
  const basicForm = (
    <Form layout="vertical" style={{ maxWidth: 480 }}>
      <Form.Item label="站点名称">
        <Input disabled placeholder="app.site_name" />
      </Form.Item>
      <Form.Item label="默认时区">
        <Input disabled placeholder="app.timezone" />
      </Form.Item>
      <Form.Item label="数据保留天数">
        <Input disabled placeholder="retention.days" />
      </Form.Item>
    </Form>
  );

  const pushForm = (
    <Form layout="vertical" style={{ maxWidth: 480 }}>
      <Form.Item label="Webhook URL">
        <Input disabled placeholder="notify.webhook_url" />
      </Form.Item>
      <Form.Item label="订单事件推送">
        <Input disabled placeholder="notify.order_events" />
      </Form.Item>
    </Form>
  );

  const aiForm = (
    <Form layout="vertical" style={{ maxWidth: 480 }}>
      <Form.Item label="模型端点">
        <Input disabled placeholder="ai.endpoint" />
      </Form.Item>
      <Form.Item label="每日建议开关">
        <Input disabled placeholder="ai.suggestions_enabled" />
      </Form.Item>
    </Form>
  );

  const autoForm = (
    <Form layout="vertical" style={{ maxWidth: 480 }}>
      <Form.Item label="自动擦亮">
        <Input disabled placeholder="auto.xianyu_polish" />
      </Form.Item>
      <Form.Item label="定时同步订单">
        <Input disabled placeholder="auto.sync_orders_cron" />
      </Form.Item>
    </Form>
  );

  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>
        系统设置
      </Title>
      <Paragraph type="secondary">
        以下为配置项占位表单，接入后端后可改为可编辑并对接保存接口。
      </Paragraph>
      <Card style={{ marginTop: 16 }}>
        <Tabs
          items={[
            { key: 'basic', label: '基本设置', children: basicForm },
            { key: 'push', label: '推送设置', children: pushForm },
            { key: 'ai', label: 'AI设置', children: aiForm },
            { key: 'auto', label: '自动化设置', children: autoForm },
          ]}
        />
      </Card>
    </div>
  );
};

export default Settings;
