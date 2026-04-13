import React, { useState, useCallback } from 'react';
import {
  Card, Tabs, Typography, Button, Space, Tag, Spin, Descriptions,
  List, Alert, message, Row, Col, Statistic, Divider,
} from 'antd';
import {
  ReloadOutlined, CheckCircleOutlined, WarningOutlined,
  CloseCircleOutlined, BulbOutlined, FileTextOutlined,
} from '@ant-design/icons';
import api from '../../services/api';

const { Title, Paragraph, Text } = Typography;

interface SelfCheckReport {
  accounts: Record<string, unknown>;
  orders_24h: Record<string, unknown>;
  listings: Record<string, unknown>;
  issues: string[];
  [key: string]: unknown;
}

interface Suggestion {
  type: string;
  content: string;
  priority: string;
}

const AiOps: React.FC = () => {
  const [reportLoading, setReportLoading] = useState(false);
  const [report, setReport] = useState<Record<string, unknown> | null>(null);

  const [checkLoading, setCheckLoading] = useState(false);
  const [checkData, setCheckData] = useState<SelfCheckReport | null>(null);

  const [sugLoading, setSugLoading] = useState(false);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);

  const fetchReport = useCallback(async () => {
    setReportLoading(true);
    try {
      const res = await api.get('/ai-ops/daily-report');
      setReport(res.data);
    } catch { message.error('获取日报失败'); }
    setReportLoading(false);
  }, []);

  const fetchSelfCheck = useCallback(async () => {
    setCheckLoading(true);
    try {
      const res = await api.get('/ai-ops/self-check');
      setCheckData(res.data);
    } catch { message.error('自检失败'); }
    setCheckLoading(false);
  }, []);

  const fetchSuggestions = useCallback(async () => {
    setSugLoading(true);
    try {
      const res = await api.get('/ai-ops/suggestions');
      setSuggestions(res.data.suggestions || []);
    } catch { message.error('获取建议失败'); }
    setSugLoading(false);
  }, []);

  const dailyTab = (
    <div>
      <Button type="primary" icon={<FileTextOutlined />} onClick={fetchReport} loading={reportLoading} style={{ marginBottom: 16 }}>
        生成今日日报
      </Button>
      {report ? (
        <Card size="small">
          <Descriptions column={2} size="small" bordered>
            {Object.entries(report).map(([key, val]) => (
              <Descriptions.Item key={key} label={key} span={typeof val === 'object' ? 2 : 1}>
                {typeof val === 'object' ? (
                  <pre style={{ fontSize: 12, margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(val, null, 2)}</pre>
                ) : (
                  String(val)
                )}
              </Descriptions.Item>
            ))}
          </Descriptions>
        </Card>
      ) : (
        <Alert type="info" message="点击上方按钮生成今日运营日报，包含订单、账号、利润等关键指标" />
      )}
    </div>
  );

  const suggestionsTab = (
    <div>
      <Button type="primary" icon={<BulbOutlined />} onClick={fetchSuggestions} loading={sugLoading} style={{ marginBottom: 16 }}>
        获取 AI 建议
      </Button>
      {suggestions.length > 0 ? (
        <List
          dataSource={suggestions}
          renderItem={(item, idx) => (
            <List.Item>
              <List.Item.Meta
                avatar={<BulbOutlined style={{ fontSize: 20, color: '#faad14' }} />}
                title={
                  <Space>
                    <Tag color={item.priority === 'high' ? 'red' : item.priority === 'medium' ? 'orange' : 'blue'}>
                      {item.priority === 'high' ? '高优先' : item.priority === 'medium' ? '中优先' : '低优先'}
                    </Tag>
                    <Tag>{item.type}</Tag>
                  </Space>
                }
                description={<Text>{item.content}</Text>}
              />
            </List.Item>
          )}
        />
      ) : (
        <Alert type="info" message="点击上方按钮，AI 将基于当前数据生成运营优化建议" />
      )}
    </div>
  );

  const selfCheckTab = (
    <div>
      <Button type="primary" icon={<CheckCircleOutlined />} onClick={fetchSelfCheck} loading={checkLoading} style={{ marginBottom: 16 }}>
        执行自检
      </Button>
      {checkData ? (
        <>
          {checkData.issues && checkData.issues.length > 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={`发现 ${checkData.issues.length} 个问题`}
              description={
                <ul style={{ margin: 0, paddingLeft: 20 }}>
                  {checkData.issues.map((issue, i) => <li key={i}>{issue}</li>)}
                </ul>
              }
            />
          )}
          <Row gutter={16} style={{ marginBottom: 16 }}>
            {checkData.accounts && (
              <Col span={8}>
                <Card size="small" title="账号">
                  <pre style={{ fontSize: 12, margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(checkData.accounts, null, 2)}</pre>
                </Card>
              </Col>
            )}
            {checkData.orders_24h && (
              <Col span={8}>
                <Card size="small" title="24h 订单">
                  <pre style={{ fontSize: 12, margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(checkData.orders_24h, null, 2)}</pre>
                </Card>
              </Col>
            )}
            {checkData.listings && (
              <Col span={8}>
                <Card size="small" title="商品">
                  <pre style={{ fontSize: 12, margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(checkData.listings, null, 2)}</pre>
                </Card>
              </Col>
            )}
          </Row>
        </>
      ) : (
        <Alert type="info" message="点击上方按钮执行系统自检，检查账号、订单、库存等核心指标" />
      )}
    </div>
  );

  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>AI 运营中枢</Title>
      <Paragraph type="secondary">运营日报、AI 建议、系统自检，一键掌握运营全局</Paragraph>

      <Card style={{ marginTop: 16 }}>
        <Tabs
          items={[
            { key: 'daily', label: '运营日报', children: dailyTab },
            { key: 'suggestions', label: 'AI 建议', children: suggestionsTab },
            { key: 'selfcheck', label: '每日自检', children: selfCheckTab },
          ]}
        />
      </Card>
    </div>
  );
};

export default AiOps;
