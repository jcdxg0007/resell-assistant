import React, { useState, useEffect, useCallback } from 'react';
import {
  Card, Form, InputNumber, Slider, Button, Space, Typography,
  Divider, Row, Col, Alert, Tooltip, App, Switch,
} from 'antd';
import { SaveOutlined, ReloadOutlined, InfoCircleOutlined } from '@ant-design/icons';
import api from '../../services/api';

const { Title, Paragraph } = Typography;

interface ParamSpec {
  type: 'int' | 'float' | 'bool';
  min?: number;
  max?: number;
  step?: number;
  label: string;
  group: string;
  help: string;
  pair?: string;       // 该字段是某区间的下限，pair 指向对应上限字段
  pair_min?: string;   // 该字段是上限，pair_min 指向对应下限字段
}
interface Specs {
  params: Record<string, ParamSpec>;
  defaults: Record<string, number | boolean>;
  groups: string[];
}

interface Props {
  /** 嵌入抽屉/控制窗口时为 true：隐藏页面级标题（由外层容器提供标题）。 */
  embedded?: boolean;
}

/**
 * PDD 采集节奏配置。数据驱动：表单完全按后端 /pdd-worker-config/specs
 * 返回的参数元数据（范围/标签/help/分组）自动渲染，后端加参数前端无需改。
 * 保存只 PUT 改动过的字段（保持后端"DB 只存被改过项"的语义）。
 *
 * 既可作独立页，也可通过 embedded 嵌进「多平台比价」页的采集节奏控制窗口。
 */
const PddConfig: React.FC<Props> = ({ embedded = false }) => {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [specs, setSpecs] = useState<Specs | null>(null);
  const [baseline, setBaseline] = useState<Record<string, number | boolean>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [specRes, cfgRes] = await Promise.all([
        api.get('/pdd-worker-config/specs'),
        api.get('/pdd-worker-config/'),
      ]);
      setSpecs(specRes.data);
      setBaseline(cfgRes.data);
      form.setFieldsValue(cfgRes.data);
    } catch {
      message.error('加载配置失败');
    }
    setLoading(false);
  }, [form, message]);

  useEffect(() => { load(); }, [load]);

  const onFinish = async (values: Record<string, number | boolean>) => {
    const patch: Record<string, number | boolean> = {};
    Object.keys(values).forEach((k) => {
      if (values[k] !== baseline[k]) patch[k] = values[k];
    });
    if (Object.keys(patch).length === 0) {
      message.info('没有改动');
      return;
    }
    setSaving(true);
    try {
      const res = await api.put('/pdd-worker-config/', { patch });
      setBaseline(res.data.config);
      message.success(res.data.note || '已保存，worker 将自动拉取');
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } } };
      const detail = err?.response?.data?.detail;
      message.error(detail ? `保存失败：${detail}` : '保存失败');
    }
    setSaving(false);
  };

  const resetDefaults = () => {
    if (specs) {
      form.setFieldsValue(specs.defaults);
      message.info('已填入默认值，点保存后生效');
    }
  };

  const renderControl = (key: string, spec: ParamSpec) => {
    if (spec.type === 'bool') {
      return <Switch checkedChildren="开" unCheckedChildren="关" />;
    }
    if (key === 'humanize_pace') {
      return (
        <Slider
          min={spec.min}
          max={spec.max}
          step={spec.step || 0.05}
          marks={{ [spec.min as number]: `${spec.min}`, 0.7: '0.7', [spec.max as number]: `${spec.max}` }}
          tooltip={{ formatter: (v) => `${v}` }}
        />
      );
    }
    return (
      <InputNumber
        min={spec.min}
        max={spec.max}
        step={spec.step || 1}
        style={{ width: '100%' }}
      />
    );
  };

  const renderField = (key: string, spec: ParamSpec) => (
    <Form.Item
      key={key}
      name={key}
      valuePropName={spec.type === 'bool' ? 'checked' : 'value'}
      label={
        <Space size={4}>
          {spec.label}
          <Tooltip title={spec.help}>
            <InfoCircleOutlined style={{ color: '#999' }} />
          </Tooltip>
        </Space>
      }
      extra={spec.type === 'bool' ? undefined : `范围 ${spec.min} ~ ${spec.max}`}
    >
      {renderControl(key, spec)}
    </Form.Item>
  );

  const renderGroup = (group: string) => {
    if (!specs) return null;
    const keys = Object.keys(specs.params).filter((k) => specs.params[k].group === group);
    const rendered = new Set<string>();
    const rows: React.ReactNode[] = [];
    keys.forEach((key) => {
      if (rendered.has(key)) return;
      const spec = specs.params[key];
      if (spec.pair && keys.includes(spec.pair)) {
        rendered.add(key);
        rendered.add(spec.pair);
        rows.push(
          <Row gutter={24} key={key}>
            <Col xs={24} sm={12}>{renderField(key, spec)}</Col>
            <Col xs={24} sm={12}>{renderField(spec.pair, specs.params[spec.pair])}</Col>
          </Row>,
        );
      } else {
        rendered.add(key);
        rows.push(renderField(key, spec));
      }
    });
    return (
      <div key={group}>
        <Divider orientation="left" style={{ fontWeight: 600 }}>{group}</Divider>
        {rows}
      </div>
    );
  };

  const body = (
    <>
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message="谨慎调整反爬相关参数"
        description="波间静默、每日配额、节奏因子调得过激进会增加账号被风控的风险。新号建议保守，稳定后再放宽。"
      />
      <Card loading={loading} variant={embedded ? 'borderless' : 'outlined'} styles={embedded ? { body: { padding: 0 } } : undefined}>
        <Form
          form={form}
          layout="vertical"
          onFinish={onFinish}
          style={{ maxWidth: embedded ? '100%' : 720 }}
        >
          {specs?.groups.map((g) => renderGroup(g))}
          <Divider />
          <Space>
            <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={saving}>
              保存
            </Button>
            <Button icon={<ReloadOutlined />} onClick={resetDefaults}>
              重置为默认值
            </Button>
            <Button onClick={load} disabled={saving}>
              放弃改动
            </Button>
          </Space>
        </Form>
      </Card>
    </>
  );

  if (embedded) {
    return body;
  }

  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>采集节奏配置</Title>
      <Paragraph type="secondary">
        调整 PDD 采集 worker 的拟人化节奏与配额。保存后家里 worker 会在下个心跳周期（≤45s）自动拉取生效，无需重启。
      </Paragraph>
      {body}
    </div>
  );
};

export default PddConfig;
