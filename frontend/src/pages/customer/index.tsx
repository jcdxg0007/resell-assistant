import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Card, Col, Row, Typography, List, Badge, Tag, Input, Button, Space,
  message, Empty, Spin, Avatar, Tooltip,
} from 'antd';
import { SendOutlined, RobotOutlined, UserOutlined, ReloadOutlined } from '@ant-design/icons';
import api from '../../services/api';

const { Title, Paragraph, Text } = Typography;

interface Conversation {
  id: string;
  platform: string;
  buyer_name: string;
  status: string;
  priority: string;
  unread_count: number;
  intent: string | null;
  last_message_at: string | null;
}

interface MessageItem {
  id: string;
  role: string;
  content: string;
  message_type: string;
  ai_generated: boolean;
  ai_approved: boolean | null;
  sent_at: string | null;
}

const INTENT_MAP: Record<string, { label: string; color: string }> = {
  inquiry: { label: '咨询', color: 'blue' },
  bargain: { label: '议价', color: 'orange' },
  complaint: { label: '投诉', color: 'red' },
  logistics: { label: '物流', color: 'cyan' },
  return: { label: '退货', color: 'volcano' },
  positive: { label: '正面', color: 'green' },
};

const Customer: React.FC = () => {
  const [convLoading, setConvLoading] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [totalUnread, setTotalUnread] = useState(0);
  const [selectedConv, setSelectedConv] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<MessageItem[]>([]);
  const [msgLoading, setMsgLoading] = useState(false);
  const [inputValue, setInputValue] = useState('');
  const [sending, setSending] = useState(false);
  const [aiDrafting, setAiDrafting] = useState(false);
  const [aiDraft, setAiDraft] = useState('');
  const msgEndRef = useRef<HTMLDivElement>(null);

  const fetchConversations = useCallback(async () => {
    setConvLoading(true);
    try {
      const res = await api.get('/customer/conversations', { params: { page_size: 50 } });
      setConversations(res.data.items || []);
      setTotalUnread(res.data.total_unread || 0);
    } catch { /* */ }
    setConvLoading(false);
  }, []);

  useEffect(() => { fetchConversations(); }, [fetchConversations]);

  const fetchMessages = useCallback(async (convId: string) => {
    setMsgLoading(true);
    try {
      const res = await api.get(`/customer/conversations/${convId}/messages`, { params: { page_size: 100 } });
      setMessages(res.data.messages || []);
      fetchConversations();
    } catch { /* */ }
    setMsgLoading(false);
    setTimeout(() => msgEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 100);
  }, [fetchConversations]);

  const handleSelectConv = (conv: Conversation) => {
    setSelectedConv(conv);
    setAiDraft('');
    setInputValue('');
    fetchMessages(conv.id);
  };

  const handleSend = async () => {
    if (!inputValue.trim() || !selectedConv) return;
    setSending(true);
    try {
      await api.post(`/customer/conversations/${selectedConv.id}/send`, { content: inputValue });
      setInputValue('');
      fetchMessages(selectedConv.id);
    } catch { message.error('发送失败'); }
    setSending(false);
  };

  const handleAiDraft = async () => {
    if (!selectedConv || !messages.length) return;
    setAiDrafting(true);
    try {
      const lastBuyerMsg = [...messages].reverse().find(m => m.role === 'buyer');
      if (!lastBuyerMsg) { message.info('没有买家消息'); setAiDrafting(false); return; }
      const res = await api.post('/customer/ai-reply', { message: lastBuyerMsg.content });
      setAiDraft(res.data.reply || '');
      setInputValue(res.data.reply || '');
    } catch { message.error('AI 生成失败'); }
    setAiDrafting(false);
  };

  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>客服中心</Title>
      <Paragraph type="secondary">
        管理买家会话，支持 AI 自动预回复
        {totalUnread > 0 && <Badge count={totalUnread} style={{ marginLeft: 8 }} />}
      </Paragraph>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col xs={24} md={8} lg={7}>
          <Card
            title="会话列表"
            style={{ height: 560 }}
            bodyStyle={{ padding: 0, overflow: 'auto', maxHeight: 500 }}
            extra={<Button size="small" icon={<ReloadOutlined />} onClick={fetchConversations} />}
          >
            {convLoading ? (
              <div style={{ textAlign: 'center', padding: 40 }}><Spin /></div>
            ) : conversations.length === 0 ? (
              <Empty description="暂无会话" style={{ padding: 40 }} />
            ) : (
              <List
                dataSource={conversations}
                renderItem={(conv) => (
                  <List.Item
                    key={conv.id}
                    onClick={() => handleSelectConv(conv)}
                    style={{
                      cursor: 'pointer',
                      padding: '10px 16px',
                      background: selectedConv?.id === conv.id ? '#e6f4ff' : undefined,
                    }}
                  >
                    <List.Item.Meta
                      avatar={<Avatar icon={<UserOutlined />} />}
                      title={
                        <Space>
                          <span>{conv.buyer_name || '未知买家'}</span>
                          {conv.unread_count > 0 && <Badge count={conv.unread_count} size="small" />}
                        </Space>
                      }
                      description={
                        <Space size={4}>
                          <Tag style={{ fontSize: 11 }}>{conv.platform === 'xianyu' ? '闲鱼' : conv.platform}</Tag>
                          {conv.intent && INTENT_MAP[conv.intent] && (
                            <Tag color={INTENT_MAP[conv.intent].color} style={{ fontSize: 11 }}>{INTENT_MAP[conv.intent].label}</Tag>
                          )}
                        </Space>
                      }
                    />
                  </List.Item>
                )}
              />
            )}
          </Card>
        </Col>

        <Col xs={24} md={16} lg={17}>
          <Card
            title={selectedConv ? `${selectedConv.buyer_name || '买家'} — ${selectedConv.platform === 'xianyu' ? '闲鱼' : selectedConv.platform}` : '消息详情'}
            style={{ height: 560, display: 'flex', flexDirection: 'column' }}
            bodyStyle={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', padding: 0 }}
          >
            {!selectedConv ? (
              <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <Text type="secondary">选择左侧会话查看消息</Text>
              </div>
            ) : (
              <>
                <div style={{ flex: 1, overflow: 'auto', padding: '12px 16px' }}>
                  {msgLoading ? <Spin style={{ display: 'block', margin: '40px auto' }} /> : (
                    messages.map((m) => (
                      <div
                        key={m.id}
                        style={{
                          display: 'flex',
                          justifyContent: m.role === 'seller' ? 'flex-end' : 'flex-start',
                          marginBottom: 10,
                        }}
                      >
                        <div
                          style={{
                            maxWidth: '70%',
                            padding: '8px 12px',
                            borderRadius: 8,
                            background: m.role === 'seller' ? '#e6f4ff' : '#f5f5f5',
                            position: 'relative',
                          }}
                        >
                          <div style={{ fontSize: 13 }}>{m.content}</div>
                          <div style={{ fontSize: 11, color: '#999', marginTop: 4 }}>
                            {m.sent_at ? new Date(m.sent_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''}
                            {m.ai_generated && <Tag color="purple" style={{ fontSize: 10, marginLeft: 4 }}>AI</Tag>}
                          </div>
                        </div>
                      </div>
                    ))
                  )}
                  <div ref={msgEndRef} />
                </div>

                <div style={{ borderTop: '1px solid #f0f0f0', padding: '8px 12px' }}>
                  {aiDraft && (
                    <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 4 }}>
                      <RobotOutlined /> AI 建议回复（已填入输入框，可编辑后发送）
                    </div>
                  )}
                  <Space.Compact style={{ width: '100%' }}>
                    <Input
                      placeholder="输入回复内容..."
                      value={inputValue}
                      onChange={(e) => setInputValue(e.target.value)}
                      onPressEnter={handleSend}
                    />
                    <Tooltip title="AI 预回复">
                      <Button icon={<RobotOutlined />} onClick={handleAiDraft} loading={aiDrafting} />
                    </Tooltip>
                    <Button type="primary" icon={<SendOutlined />} onClick={handleSend} loading={sending}>
                      发送
                    </Button>
                  </Space.Compact>
                </div>
              </>
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );
};

export default Customer;
