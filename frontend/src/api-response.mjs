function serviceUnavailableMessage(status) {
  return `服务请求失败（HTTP ${status}），请确认 LoopForge 后端已启动`;
}

export async function parseApiResponse(response) {
  const body = await response.text();
  if (!body.trim()) {
    if (!response.ok) {
      throw new Error(serviceUnavailableMessage(response.status));
    }
    throw new Error("服务返回空响应，请刷新重试");
  }

  let payload;
  try {
    payload = JSON.parse(body);
  } catch {
    if (!response.ok) {
      throw new Error(serviceUnavailableMessage(response.status));
    }
    throw new Error("服务返回了无效 JSON，请刷新重试");
  }

  if (!response.ok) {
    throw new Error(payload?.summary || `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}
