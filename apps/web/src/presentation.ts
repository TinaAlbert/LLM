// Display labels only. API identifiers, amounts and action rules remain unchanged.
export const money = (amount: number | null | undefined) =>
  amount == null || !Number.isFinite(amount) ? '金额待确认' :
    `${(amount / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 模拟元`;

const states: Record<string, string> = {
  draft: '待预订', payment_pending: '等待付款结果', processing: '处理中',
  unknown: '付款结果未知', paid: '已付款', succeeded: '成功', failed: '失败',
  pending: '待处理', issued: '已出票', valid: '有效', held: '已占用',
  protected: '付款处理中，资源已保留', consumed: '已消费', available: '可用',
  cancelled: '已取消', invalidated: '已失效', expired: '已到期', approved: '已批准',
  rejected: '已拒绝', active: '有效', released: '已释放',
};
export const status = (value: string | null | undefined) => states[value ?? ''] ?? '状态待确认';
export const paymentStatus = (value: string) => ({
  processing: '付款处理中', unknown: '付款结果未知', succeeded: '付款成功', failed: '付款失败',
}[value] ?? '付款状态待确认');
export const ticketStatus = (value: string) => ({
  pending: '等待出票', processing: '出票处理中', succeeded: '出票处理完成',
  failed: '出票失败', issued: '出票处理完成',
}[value] ?? '出票状态待确认');
export const tone = (value: string | null | undefined) =>
  ['failed', 'rejected'].includes(value ?? '') ? 'danger' :
  ['valid', 'issued', 'approved', 'succeeded', 'available'].includes(value ?? '') ? 'success' :
  ['unknown', 'payment_pending', 'processing', 'pending', 'expired', 'invalidated'].includes(value ?? '') ? 'warning' : 'neutral';

export const flightName = (id: string) => ({
  'DEMO-PEK-SHA': '北京 → 上海', 'DEMO-LARGE': '行程 A', 'DEMO-SMALL': '行程 B',
}[id] ?? (id.startsWith('DEMO-') ? '模拟航段' : id));

export const projectName = (id: string | null | undefined) => !id ? '尚未选择' : ({
  'A-eng': 'A 组织 · 工程差旅', 'A-eng-alt': 'A 组织 · 工程备用项目',
  'A-sales': 'A 组织 · 销售差旅', 'B-eng': 'B 组织 · 工程差旅',
}[id] ?? '差旅预算项目');

export function sourceName(ref: string | null, sources: {ref: string; kind: string}[]) {
  if (!ref) return '尚未绑定';
  const source = sources.find(item => item.ref === ref);
  if (!source) return '已绑定付款资料';
  const peers = sources.filter(item => item.kind === source.kind);
  const kind = source.kind === 'corporate' ? '企业付款资料' : source.kind === 'personal' ? '个人付款资料' : '付款资料';
  return `${kind}${peers.length > 1 ? ` ${peers.findIndex(item => item.ref === ref) + 1}` : ''}`;
}

export function businessTime(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? '时间待确认' : parsed.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  });
}
