const phases: Record<string, string> = {
  connect: '建立语音连接',
  submit_task: '提交语音任务',
  await_start: '等待供应商任务开始',
  submit_text: '提交台词',
  receive_audio: '接收音频',
  deliver_audio: '发送音频',
  authorize: '检查语音授权',
  reserve: '预留费用',
  validate_pcm: '检查音频完整性',
  commit_pcm: '保存音频',
  unknown: '未定位',
}

function closeCode(value: unknown) {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1000 && value <= 4999 ? value : null
}

export function VoiceFailureDetails({ diagnostic }: { diagnostic: unknown }) {
  if (!diagnostic || typeof diagnostic !== 'object') return null
  const fields = diagnostic as Record<string, unknown>
  if (typeof fields.failure_type !== 'string' || !/^[A-Za-z0-9_]{1,80}$/.test(fields.failure_type))
    return null
  const phase = typeof fields.failure_phase === 'string' ? phases[fields.failure_phase] || '未定位' : '未定位'
  const received = closeCode(fields.received_close_code)
  const sent = closeCode(fields.sent_close_code)
  return (
    <details>
      <summary>语音失败诊断 · {phase}</summary>
      <p>异常类别：{fields.failure_type}</p>
      {received !== null && <p>收到的 WebSocket 关闭码：{received}</p>}
      {sent !== null && <p>发出的 WebSocket 关闭码：{sent}</p>}
      {received !== null && sent !== null && typeof fields.received_close_first === 'boolean' && (
        <p>关闭顺序：{fields.received_close_first ? '先收到关闭帧' : '先发出关闭帧'}</p>
      )}
      <p className="muted">仅记录有限的传输元数据；关闭码不能单独确定失败原因。</p>
    </details>
  )
}
