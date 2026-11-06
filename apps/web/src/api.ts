// 会话仅在内存；每次身份切换使所有旧请求失效。
export class StaleResponse extends Error {}
export class ApiError extends Error {
  constructor(public code: string, public status: number) { super(code); }
}
export interface Operation { method: string; path: string; body: unknown; key: string; label: string }
export class Api {
  private token = '';
  private generation = 0;
  private controllers = new Set<AbortController>();
  switchIdentity() {
    this.generation++;
    for (const controller of this.controllers) controller.abort();
    this.controllers.clear();
    const previous = this.token;
    this.token = '';
    return { previous, generation: this.generation };
  }
  async login(account: string, previous: string, generation: number) {
    let result;
    try { result = await this.request<{access_token: string}>('/demo/sessions', 'POST', {account}, undefined, previous); }
    catch (error) {
      if (!(error instanceof ApiError && error.status === 401 && previous)) throw error;
      result = await this.request<{access_token: string}>('/demo/sessions', 'POST', {account}, undefined, '');
    }
    if (generation !== this.generation) throw new StaleResponse();
    this.token = result.access_token;
  }
  async request<T>(path: string, method = 'GET', body?: unknown, key?: string, token = this.token,
    options?: {signal?: AbortSignal; timeoutMs?: number}): Promise<T> {
    const generation = this.generation;
    const controller = new AbortController();
    this.controllers.add(controller);
    const abort = () => controller.abort();
    if(options?.signal?.aborted)controller.abort();
    options?.signal?.addEventListener('abort',abort);
    const timer = setTimeout(abort, options?.timeoutMs ?? 20000);
    try {
      const response = await fetch('/api' + path, {
        method, cache: 'no-store', credentials: 'omit', signal: controller.signal,
        headers: { ...(token ? {Authorization: `Bearer ${token}`} : {}),
          ...(body !== undefined ? {'Content-Type': 'application/json'} : {}), ...(key ? {'Idempotency-Key': key} : {}) },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const data = response.status === 204 ? null : await response.json();
      if (generation !== this.generation) throw new StaleResponse();
      if (!response.ok) throw new ApiError(typeof data?.error === 'string' ? data.error : 'request_failed', response.status);
      return data as T;
    } catch (error) {
      if (generation !== this.generation) throw new StaleResponse();
      if (error instanceof ApiError || error instanceof StaleResponse) throw error;
      throw new ApiError('network_unknown', 0);
    } finally { clearTimeout(timer); options?.signal?.removeEventListener('abort',abort);this.controllers.delete(controller); }
  }
  guide<T>(body: unknown, signal?: AbortSignal) {
    return this.request<T>('/guide/next','POST',body,undefined,this.token,{signal,timeoutMs:30500});
  }
  write<T>(operation: Operation) { return this.request<T>(operation.path, operation.method, operation.body, operation.key); }
}

const messages: Record<string, string> = {
  authentication_required: '会话已失效，请重新登录。', not_visible: '对象不存在或当前身份无权访问。',
  state_changed: '状态已变化，已重新读取。请核对当前修订再操作。', quote_expired: '报价已到期，请重新报价。',
  requires_payment_profile: '请先绑定本人的付款资料。', payment_profile_ineligible: '该付款资料当前不适用。',
  requires_approval: '请等待此锁价对应的申请获批。', requires_corporate_quote: '请先取得有效企业报价。',
  requires_budget: '请先选择预算项目。', budget_insufficient: '预算可用额度不足。',
  inventory_unavailable: '当前库存不足，请重新选择航段或日期。', flight_unavailable: '航段当前不可用。',
  amount_mismatch: '付款金额与最终锁价不同，请重新核对。', active_lock_exists: '此行程已有活动锁价。',
  active_approval_exists: '已有活动申请，请读取原申请。', payment_exists: '已有付款，请查询原付款状态。',
  payment_protected: '付款已受理，当前不能修改或释放相关资源。', invalid_request: '输入不符合要求，请检查。',
  idempotency_conflict: '重试参数与原操作不同，请读取当前状态。',
  storage_unavailable: '服务暂不可用。写入结果需确认，请重试原操作。',
  network_unknown: '连接中断，尚不能确认结果。请重试原操作，不要重复提交新操作。',
};
export function explain(error: unknown) { return error instanceof ApiError ? messages[error.code] ?? `操作未完成（${error.code}）。` : '操作未完成，请刷新后核对。'; }
