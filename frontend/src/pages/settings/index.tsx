import React, { useState, useEffect, useCallback } from 'react';
import {
  Card, Form, Input, Tabs, Typography, Switch, Button, Space, message,
  Tag, Descriptions, Alert,
} from 'antd';
import { SaveOutlined, CheckCircleOutlined, CloseCircleOutlined } from '@ant-design/icons';
import api from '../../services/api';

const { Title, Paragraph, Text } = Typography;

interface SystemSettings {
  auto_purchase_mode: string;
  dingtalk_webhook_url: string;
  dingtalk_secret: string;
  [key: string]: string;
}

const Settings: React.FC = () => {
  const [settings, setSettings] = useState<SystemSettings>({
    auto_purchase_mode: 'manual',
    dingtalk_webhook_url: '',
    dingtalk_secret: '',
  });
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);

  const fetchSettings = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get('/settings/');
      setSettings((prev) => ({ ...prev, ...res.data }));
    } catch {
      /* ignore */
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchSettings();
  }, [fetchSettings]);

  const saveSetting = async (key: string, value: string) => {
    setSaving(key);
    try {
      await api.put('/settings/', { key, value });
      setSettings((prev) => ({ ...prev, [key]: value }));
      message.success('已保存');
    } catch {
      message.error('保存失败');
    }
    setSaving(null);
  };

  const isAutoMode = settings.auto_purchase_mode === 'auto';

  const purchaseTab = (
    <div style={{ maxWidth: 560 }}>
      <Descriptions column={1} bordered size="small" style={{ marginBottom: 16 }}>
        <Descriptions.Item label="当前模式">
          {isAutoMode ? (
            <Tag color="blue">自动采购</Tag>
          ) : (
            <Tag color="orange">半自动（钉钉通知）</Tag>
          )}
        </Descriptions.Item>
      </Descriptions>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <Text strong>自动采购：</Text>
        <Switch
          checked={isAutoMode}
          loading={saving === 'auto_purchase_mode'}
          onChange={(checked) => saveSetting('auto_purchase_mode', checked ? 'auto' : 'manual')}
          checkedChildren="自动"
          unCheckedChildren="半自动"
        />
      </div>

      <Alert
        type={isAutoMode ? 'info' : 'success'}
        showIcon
        message={
          isAutoMode
            ? '自动模式：检测到新订单后，系统自动在源平台下单采购'
            : '半自动模式：检测到新订单后，推送钉钉通知（含收货地址和货源链接），由您手动采购'
        }
        description={
          isAutoMode
            ? '适用于单量大且采购账号稳定的场景，有被平台风控的风险'
            : '安全稳定，推荐前期使用。在订单管理页录入采购信息即可'
        }
      />
    </div>
  );

  const [webhookForm] = Form.useForm();
  useEffect(() => {
    webhookForm.setFieldsValue({
      dingtalk_webhook_url: settings.dingtalk_webhook_url,
      dingtalk_secret: settings.dingtalk_secret,
    });
  }, [settings, webhookForm]);

  const pushForm = (
    <div style={{ maxWidth: 560 }}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="钉钉机器人配置"
        description="在钉钉群中添加自定义机器人（Webhook），获取 Webhook URL 和 加签密钥。半自动模式下新订单通知会推送到此群。"
      />

      <Form
        form={webhookForm}
        layout="vertical"
        onFinish={async (values) => {
          setSaving('dingtalk');
          try {
            await api.put('/settings/', { key: 'dingtalk_webhook_url', value: values.dingtalk_webhook_url || '' });
            await api.put('/settings/', { key: 'dingtalk_secret', value: values.dingtalk_secret || '' });
            setSettings((prev) => ({
              ...prev,
              dingtalk_webhook_url: values.dingtalk_webhook_url || '',
              dingtalk_secret: values.dingtalk_secret || '',
            }));
            message.success('钉钉配置已保存');
          } catch {
            message.error('保存失败');
          }
          setSaving(null);
        }}
      >
        <Form.Item label="Webhook URL" name="dingtalk_webhook_url">
          <Input placeholder="https://oapi.dingtalk.com/robot/send?access_token=..." />
        </Form.Item>
        <Form.Item label="加签密钥（Secret）" name="dingtalk_secret">
          <Input.Password placeholder="SEC..." />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={saving === 'dingtalk'}>
              保存
            </Button>
            <Text type="secondary">
              状态：{settings.dingtalk_webhook_url ? (
                <Tag icon={<CheckCircleOutlined />} color="success">已配置</Tag>
              ) : (
                <Tag icon={<CloseCircleOutlined />} color="default">未配置</Tag>
              )}
            </Text>
          </Space>
        </Form.Item>
      </Form>
    </div>
  );

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

  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>
        系统设置
      </Title>
      <Paragraph type="secondary">
        管理采购模式、推送通知等核心配置
      </Paragraph>
      <Card style={{ marginTop: 16 }} loading={loading}>
        <Tabs
          items={[
            { key: 'purchase', label: '采购模式', children: purchaseTab },
            { key: 'push', label: '推送通知', children: pushForm },
            { key: 'basic', label: '基本设置', children: basicForm },
            { key: 'ai', label: 'AI设置', children: aiForm },
          ]}
        />
      </Card>
    </div>
  );
};

export default Settings;
