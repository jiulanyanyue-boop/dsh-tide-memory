/**
 * dsh-tide-memory · 客户端半侧（设置页）
 *
 * 交互约定（按主人要求）：
 *   · 用户只写/改「记忆内容」的自然语言；
 *   · kind / predicate / object / 状态 / 置信度全部由 LLM 判定，界面上只读展示；
 *   · 新增与编辑都走弹窗（portal 挂到 body，不受设置面板容器影响），主页面只留列表卡片。
 *
 * 样式注入采用「整段替换」而不是「有就跳过」：旧 bundle 残留的 style 节点
 * 会让同一 ID 的新 CSS 永远进不去（上次弹窗退化成内联就是这个原因）。
 */

window.__ModuleLoader__.load({
  id: 'dsh-tide-memory',
  factory: function (require) {
    var module = { exports: {} }
    var exports = module.exports

    var API = '/plugins/dsh-tide-memory/api'
    var PKG = 'dsh-tide-memory'
    var STYLE_ID = 'dsh-tide-memory-style'

    /* ── API 调用：带 CSRF token ──────────────────────────────────────────
     *
     * 宿主 webServer 不做鉴权，插件侧自己扛。流程：
     *   GET /plugins/dsh-tide-memory/api  → { token }
     *   POST + header `x-dsh-tide-token`  → 正常响应
     *
     * token 每次进程启动都会变，所以失败一次就清缓存重取。
     */
    var apiToken = null
    var apiTokenPending = null

    function fetchToken(force) {
      if (!force && apiToken) return Promise.resolve(apiToken)
      if (!force && apiTokenPending) return apiTokenPending
      apiTokenPending = fetch(API, {
        method: 'GET',
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      })
        .then(function (r) {
          return r.json()
        })
        .then(function (data) {
          apiToken = (data && data.token) || ''
          apiTokenPending = null
          return apiToken
        })
        .catch(function (error) {
          apiTokenPending = null
          throw error
        })
      return apiTokenPending
    }

    function apiCall(payload, isRetry) {
      return fetchToken(!!isRetry).then(function (token) {
        return fetch(API, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'x-dsh-tide-token': token || '',
          },
          credentials: 'same-origin',
          body: JSON.stringify(payload),
        })
          .then(function (r) {
            // token 过期（进程重启过）→ 重新取一次再试
            if (r.status === 401 && !isRetry) {
              return apiCall(payload, true)
            }
            return r.json()
          })
      })
    }

    var CSS = [
      '.dm-root{padding:6px 4px 36px;font-size:13.5px;line-height:1.6;color:var(--dsw-alias-label-primary,inherit);max-width:1000px}',
      '.dm-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 2px;flex-wrap:wrap}',
      '.dm-head h2{margin:0;font-size:19px;font-weight:600;letter-spacing:.01em;display:flex;align-items:center;gap:8px}',
      '.dm-sub{color:var(--dsw-alias-label-secondary,#8b8b8b);margin:0 0 16px;font-size:12.5px}',
      '.dm-card{border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.26));border-radius:14px;padding:14px 16px;margin-bottom:14px;background:var(--dsw-alias-button-elevated-fill,transparent);box-shadow:0 1px 2px rgba(0,0,0,.03)}',
      '.dm-card-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px;flex-wrap:wrap}',
      '.dm-card-title{font-weight:600;font-size:13.5px;display:flex;align-items:center;gap:8px}',
      '.dm-dot{width:7px;height:7px;border-radius:50%;display:inline-block;flex:none}',
      '.dm-dot.on{background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.16)}',
      '.dm-dot.off{background:#9ca3af}',
      '.dm-btn{height:32px;padding:0 14px;border-radius:9px;border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.3));background:transparent;color:inherit;font:inherit;cursor:pointer;transition:background .15s,border-color .15s,opacity .15s,transform .12s;white-space:nowrap}',
      '.dm-btn:hover:not(:disabled){background:var(--dsw-alias-button-floating-hover,rgba(128,128,128,.12))}',
      '.dm-btn:active:not(:disabled){transform:scale(.98)}',
      '.dm-btn.primary{background:var(--dsw-alias-brand-primary,#4d6bfe);border-color:transparent;color:var(--dsw-alias-label-primary-inverted,#fff)}',
      '.dm-btn.primary:hover:not(:disabled){background:var(--dsw-alias-button-primary-hover,#3f57cc)}',
      '.dm-btn:disabled{opacity:.45;cursor:not-allowed}',
      '.dm-btn.tiny{height:26px;padding:0 10px;font-size:12px}',
      '.dm-list{display:flex;flex-direction:column}',
      '.dm-item{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:11px 2px;border-bottom:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.12))}',
      '.dm-item:last-child{border-bottom:none}',
      '.dm-item-main{min-width:0;flex:1 1 auto}',
      '.dm-item-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}',
      '.dm-content{font-weight:500;word-break:break-word}',
      '.dm-item-expired .dm-content{opacity:.55;text-decoration:line-through}',
      '.dm-tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:6px;background:rgba(128,128,128,.12);color:var(--dsw-alias-label-secondary,#8b8b8b);white-space:nowrap}',
      '.dm-tag.warn{background:rgba(239,68,68,.12);color:#dc2626}',
      '.dm-raw{font-size:11px;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
      '.dm-raw summary{cursor:pointer;list-style:none;opacity:.75}',
      '.dm-raw summary:hover{opacity:1;text-decoration:underline}',
      '.dm-raw summary::-webkit-details-marker{display:none}',
      '.dm-raw code{display:block;margin-top:3px}',
      '.dm-meta{margin-top:4px;font-size:11.5px;color:var(--dsw-alias-label-tertiary,#9a9a9a);display:flex;gap:6px;flex-wrap:wrap;align-items:center}',
      '.dm-meta code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;background:rgba(128,128,128,.1);padding:1px 5px;border-radius:5px}',
      '.dm-code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}',
      '.dm-badge{display:inline-block;font-size:11px;padding:1.5px 8px;border-radius:999px;border:1px solid transparent;white-space:nowrap}',
      '.dm-badge.active{color:#16a34a;background:rgba(34,197,94,.12);border-color:rgba(34,197,94,.25)}',
      '.dm-badge.candidate{color:#d97706;background:rgba(245,158,11,.12);border-color:rgba(245,158,11,.28)}',
      '.dm-badge.superseded{color:#6b7280;background:rgba(107,114,128,.12);border-color:rgba(107,114,128,.25)}',
      '.dm-badge.rejected{color:#dc2626;background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.22)}',
      '.dm-empty{padding:30px 12px;text-align:center;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',

      /* ── 首次运行引导（Python 环境）────────────────────────────────── */
      '.dm-gate-mask{position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;justify-content:center;padding:24px;background:rgba(0,0,0,.42);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}',
      '.dm-gate{width:100%;max-width:540px;max-height:calc(100vh - 48px);overflow:auto;border-radius:18px;padding:22px 24px 18px;background:var(--dsw-alias-bg-elevated,#fff);color:var(--dsw-alias-label-primary,#111);box-shadow:0 24px 64px rgba(0,0,0,.28),0 2px 8px rgba(0,0,0,.12);border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.18))}',
      '.dm-gate-head{display:flex;gap:14px;align-items:flex-start;margin-bottom:14px}',
      '.dm-gate-emoji{font-size:32px;line-height:1;flex:none;margin-top:2px}',
      '.dm-gate-head h3{margin:0 0 4px;font-size:17px;font-weight:650;letter-spacing:.01em}',
      '.dm-gate-sub{margin:0;font-size:12.5px;color:var(--dsw-alias-label-secondary,#6b7280)}',
      '.dm-gate-body{font-size:13px;line-height:1.62;color:var(--dsw-alias-label-primary,#222)}',
      '.dm-gate-body p{margin:0 0 10px}',
      '.dm-gate-warn{padding:10px 12px;border-radius:10px;background:rgba(245,158,11,.10);border-left:3px solid #f59e0b;color:var(--dsw-alias-label-primary,#333)}',
      '.dm-gate-caps{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;margin:12px 0 10px;font-size:12.5px;color:var(--dsw-alias-label-secondary,#4b5563)}',
      '.dm-cap{display:inline-block;width:14px;text-align:center;font-weight:700}',
      '.dm-cap.on{color:#16a34a}',
      '.dm-cap.off{color:#dc2626}',
      '.dm-gate-detail{font-size:12px;color:var(--dsw-alias-label-tertiary,#6b7280);margin:6px 0 10px}',
      '.dm-gate-detail summary{cursor:pointer;opacity:.85;list-style:none}',
      '.dm-gate-detail summary::-webkit-details-marker{display:none}',
      '.dm-gate-detail summary:hover{opacity:1;text-decoration:underline}',
      '.dm-gate-detail ul{margin:6px 0 0;padding-left:16px;line-height:1.7}',
      '.dm-gate-detail code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;background:rgba(128,128,128,.12);padding:1px 5px;border-radius:4px;word-break:break-all}',
      '.dm-gate-note{margin:8px 0 0;font-size:12.5px;color:var(--dsw-alias-brand-primary,#4d6bfe)}',
      '.dm-gate-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:16px}',
      '.dm-gate-actions .dm-btn{height:34px}',
      '.dm-btn.ghost{opacity:.7}',
      '.dm-btn.ghost:hover{opacity:1}',
      '.dm-gate-hint{font-size:12px;color:var(--dsw-alias-label-tertiary,#6b7280);margin-right:4px}',
      '.dm-gate-foot{margin:12px 0 0;font-size:11.5px;line-height:1.6;color:var(--dsw-alias-label-tertiary,#9ca3af)}',
      '.dm-gate-link{color:var(--dsw-alias-brand-primary,#4d6bfe);text-decoration:none;white-space:nowrap}',
      '.dm-gate-link:hover{text-decoration:underline}',
      '@media (max-width:520px){.dm-gate-caps{grid-template-columns:1fr}.dm-gate-actions .dm-btn{flex:1 1 auto}}',
      '.dm-msg{font-size:12.5px;color:var(--dsw-alias-label-secondary,#8b8b8b)}',
      '.dm-actions{display:flex;gap:6px;flex:none}',
      '.dm-hint{color:var(--dsw-alias-label-tertiary,#9a9a9a);font-size:12px;margin-top:6px}',
      '.dm-pre{margin:8px 0 0;padding:8px 10px;border-radius:9px;background:var(--dsw-alias-button-floating-hover,rgba(128,128,128,.1));font-family:ui-monospace,monospace;font-size:11.5px;overflow-x:auto}',
      '.dm-llm-line{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;font-size:12.5px}',
      '.dm-overlay{position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;justify-content:center;padding:20px;background:rgba(0,0,0,.42);backdrop-filter:blur(2px);animation:dm-fade .15s ease}',
      '.dm-modal{width:min(560px,calc(100vw - 32px));max-height:calc(100vh - 80px);overflow:auto;border-radius:16px;border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.3));background:var(--dsw-alias-button-elevated-fill,#fff);padding:20px 22px;box-shadow:0 24px 64px rgba(0,0,0,.3);animation:dm-pop .18s cubic-bezier(.2,.9,.3,1.08)}',
      '.dm-modal h3{margin:0 0 4px;font-size:16px;font-weight:600}',
      '.dm-modal p{margin:0 0 12px;font-size:12.5px;color:var(--dsw-alias-label-secondary,#8b8b8b)}',
      '.dm-textarea{width:100%;box-sizing:border-box;min-height:118px;resize:vertical;padding:10px 12px;border-radius:10px;border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.3));background:transparent;color:inherit;font:inherit;outline:none;transition:border-color .15s,box-shadow .15s}',
      '.dm-textarea:focus{border-color:var(--dsw-alias-brand-primary,#4d6bfe);box-shadow:0 0 0 3px color-mix(in srgb,var(--dsw-alias-brand-primary,#4d6bfe) 18%,transparent)}',
      '.dm-modal-foot{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}',
      '@keyframes dm-fade{from{opacity:0}to{opacity:1}}',
      '@keyframes dm-pop{from{opacity:0;transform:translateY(10px) scale(.98)}to{opacity:1;transform:none}}',
      '@media (max-width:560px){.dm-item{flex-direction:column;align-items:stretch}.dm-actions{justify-content:flex-end}}',
      // ── 自动更新卡片 + Star 按钮 + 插件小图标
      '.dm-update{border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.26));border-radius:14px;padding:14px 16px;margin-bottom:14px;background:var(--dsw-alias-button-elevated-fill,transparent);box-shadow:0 1px 2px rgba(0,0,0,.03)}',
      '.dm-update-head{margin-bottom:10px}',
      '.dm-update-title{margin:0 0 2px;font-size:13.5px;font-weight:600;display:flex;align-items:center;gap:8px}',
      '.dm-update-hint{margin:0;font-size:12px;line-height:1.55;color:var(--dsw-alias-label-secondary,#8b8b8b)}',
      '.dm-chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}',
      '.dm-chip-group{display:inline-flex;align-items:center;gap:6px}',
      '.dm-chip-label{font-size:12px;color:var(--dsw-alias-label-secondary,#8b8b8b)}',
      '.dm-chip{border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.3));border-radius:8px;padding:1px 8px;font-size:12px;line-height:18px;display:inline-block}',
      '.dm-update-status{font-size:12px;line-height:1.5;margin-bottom:10px}',
      '.dm-update-status-ok{color:#16a34a}',
      '.dm-update-status-warn{color:#d97706}',
      '.dm-update-status-error{color:#dc2626}',
      '.dm-update-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}',
      '.dm-update-foot{margin-top:10px;font-size:11px;color:var(--dsw-alias-label-secondary,#8b8b8b)}',
      '.dm-star-glyph{color:#f59e0b;font-size:13px;line-height:1}',
      '.dm-plugin-icon{border-radius:9px;flex:none;object-fit:cover;display:block;box-shadow:0 1px 3px rgba(0,0,0,.12)}',
      '@media (prefers-reduced-motion:reduce){.dm-btn,.dm-overlay,.dm-modal{transition:none;animation:none}}',
    ].join('\n')

    /**
     * 插件图标（Q 版鲸鱼娘，使用方提供的插画切图内嵌为 data URL）。
     * 内嵌而非外链：桌面端以 file:// 加载 dist，外链图片会受 CSP/跨源限制。
     */
    var PLUGIN_ICON_DATA_URL = 'data:image/png;base64,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'
    function injectStyle() {
      if (typeof document === 'undefined') return
      // 关键：同一 ID 的旧节点要替换掉，否则新 bundle 的 CSS 永远进不来
      var existing = document.getElementById(STYLE_ID)
      if (existing && existing.parentNode) existing.parentNode.removeChild(existing)
      var style = document.createElement('style')
      style.id = STYLE_ID
      style.textContent = CSS
      document.head.appendChild(style)
    }

      /* ══ 首次运行引导：Python 环境缺失提示 ══════════════════════════════
       *
       * 只在「没有可用 Python 3.10+」时出现一次。
       * 三种退出路径，全部都会调用 props.complete()：
       *   ① 环境正常（装好了 / 本来就有）→ 静默放行
       *   ② 用户点「不再提醒」→ 记住选择，永不再弹
       *   ③ 用户点「稍后」→ 本次放行，下次启动还会提醒
       *
       * ⚠️ complete() 不调用会卡住整个 onboarding 协调器（连带阻塞主题启动画面），
       *    所以任何分支都必须走到它——包括接口失败。
       */
      function makeEnvGate(React) {
        var h = React.createElement
        var DISMISS_KEY = PKG + '.python-env.dismissed'

        function readDismissed() {
          try {
            return window.localStorage.getItem(DISMISS_KEY) === '1'
          } catch {
            return false
          }
        }
        function writeDismissed() {
          try {
            window.localStorage.setItem(DISMISS_KEY, '1')
          } catch {
            /* 隐私模式禁写，忽略 */
          }
        }
        function clearDismissed() {
          try {
            window.localStorage.removeItem(DISMISS_KEY)
          } catch {
            /* ignore */
          }
        }

        function api(payload) {
          return apiCall(payload, false)
        }

        function EnvGate(props) {
          var complete = props && props.complete
          var openSection = props && props.openSection
          var explicit = props && props.explicit

          var st = React.useState('checking') // checking | missing | installing | done
          var state = st[0]
          var setState = st[1]

          var envSt = React.useState(null)
          var env = envSt[0]
          var setEnv = envSt[1]

          var noteSt = React.useState('')
          var note = noteSt[0]
          var setNote = noteSt[1]

          // 标记「已经给过机会」，避免重复探测打扰每一次挂载
          var settled = React.useRef(false)

          var done = function () {
            if (settled.current) return
            settled.current = true
            if (typeof complete === 'function') complete()
          }

          var probe = function (isRetry) {
            if (!isRetry) setState('checking')
            return api({ op: 'env_status' })
              .then(function (res) {
                if (!res || res.ok !== true) {
                  setEnv(res || null)
                  // 环境缺失：首次弹窗；已点过「不再提醒」则静默放行
                  if (readDismissed() && !explicit) {
                    setState('done')
                    done()
                    return
                  }
                  setState('missing')
                  return
                }
                // 环境正常
                setEnv(res)
                clearDismissed()
                setState('done')
                done()
              })
              .catch(function () {
                // 接口挂了不该拦住用户，放行
                setState('done')
                done()
              })
          }

          React.useEffect(function () {
            var alive = true
            if (!alive) return
            probe(false)
            // 兜底：3 秒还没结论就放行（协调器不能被我们卡住）
            var timer = setTimeout(function () {
              if (alive && !settled.current) {
                setState('done')
                done()
              }
            }, 3000)
            return function () {
              alive = false
              clearTimeout(timer)
            }
          }, [])

          // 关闭动画：done 状态渲染 null
          if (state === 'done' || state === 'checking') return null

          var startInstall = function () {
            setState('installing')
            setNote('正在准备安装…（若弹出窗口请按提示完成）')
            api({ op: 'python_install' })
              .then(function (res) {
                if (res && res.ok) {
                  setNote(res.message || '安装程序已启动。')
                } else {
                  setNote('启动失败：' + ((res && res.error) || '未知原因'))
                  setState('missing')
                }
              })
              .catch(function (error) {
                setNote('启动失败：' + error)
                setState('missing')
              })
          }

          var recheck = function () {
            setNote('')
            probe(true)
          }

          var later = function () {
            setState('done')
            done()
          }

          var never = function () {
            writeDismissed()
            setState('done')
            done()
          }

          var tried = (env && env.tried) || []

          return h(
            'div',
            { className: 'dm-gate-mask' },
            h(
              'div',
              { className: 'dm-gate' },
              h(
                'div',
                { className: 'dm-gate-head' },
                h('span', { className: 'dm-gate-emoji' }, '🐋'),
                h('div', null,
                  h('h3', null, '记忆引擎还差一步'),
                  h('p', { className: 'dm-gate-sub' }, '检测到本机没有可用的 Python 3.10+'),
                ),
              ),

              h('div', { className: 'dm-gate-body' },
                h('p', null,
                  '记忆的', h('b', null, '采集'), '和', h('b', null, '注入'),
                  '现在就能用——你说过的话会被记下来，相关记忆会自动带回上下文。',
                ),
                h('p', { className: 'dm-gate-warn' },
                  '但「自动整理」需要 Python：它会从对话里抽取新记忆、判定冲突、清理过期条目。',
                  '没有它，记忆只会越堆越多，不会变精炼。',
                ),

                h('div', { className: 'dm-gate-caps' },
                  h('div', null, h('span', { className: 'dm-cap on' }, '✓'), ' 采集真人消息'),
                  h('div', null, h('span', { className: 'dm-cap on' }, '✓'), ' 自动注入相关记忆'),
                  h('div', null, h('span', { className: 'dm-cap off' }, '✕'), ' 从对话抽取新记忆'),
                  h('div', null, h('span', { className: 'dm-cap off' }, '✕'), ' 冲突判定 / 自动过期'),
                ),

                tried.length > 0
                  ? h('details', { className: 'dm-gate-detail' },
                      h('summary', null, '看检测过的位置'),
                      h('ul', null,
                        tried.map(function (t, i) {
                          return h('li', { key: i },
                            h('code', null, t.path),
                            ' → ',
                            t.result === 'ok' ? '可用' :
                            t.result === 'too-old' ? ('版本太老 ' + (t.version || '')) :
                            t.result === 'not-found' ? '不存在' : '跑不起来',
                          )
                        }),
                      ),
                    )
                  : null,

                note ? h('p', { className: 'dm-gate-note' }, note) : null,
              ),

              h('div', { className: 'dm-gate-actions' },
                state === 'installing'
                  ? h('span', { className: 'dm-gate-hint' }, '装好后点右边「重新检测」')
                  : h('a', {
                      className: 'dm-btn primary',
                      href: 'javascript:void(0)',
                      onClick: startInstall,
                      role: 'button',
                    }, '一键安装 Python'),
                h('button', { className: 'dm-btn', onClick: recheck }, '重新检测'),
                h('button', { className: 'dm-btn', onClick: later }, '稍后再说'),
                h('button', { className: 'dm-btn ghost', onClick: never }, '不再提醒'),
              ),

              h('p', { className: 'dm-gate-foot' },
                '安装脚本会用官方安装包装到默认位置，',
                '只影响当前用户、不需要管理员权限、不改系统环境变量。',
                openSection
                  ? h('a', {
                      className: 'dm-gate-link',
                      href: 'javascript:void(0)',
                      onClick: function () {
                        setState('done')
                        done()
                        openSection(PKG)
                      },
                    }, ' 去记忆设置页看看 →')
                  : null,
              ),
            ),
          )
        }

        return EnvGate
      }

    function makePanel(React, ReactDOM) {
      var h = React.createElement

      // 迁移 002：给人看的标签。绝不把谓词/类型码直接亮给主人。
      var KIND_CN = {
        preference: '偏好',
        fact: '事实',
        episode: '经历',
        reflection: '反思',
      }
      var EXPIRED_CN = {
        superseded: '被新值取代',
        'scope-gone': '项目已不存在',
        ttl: '太久没用到',
        manual: '手动失效',
      }
      function kindLabel(kind) {
        return KIND_CN[kind] || kind || ''
      }
      function expiredLabel(reason) {
        return EXPIRED_CN[reason] || reason || ''
      }


      function Panel() {
        var itemsState = React.useState([])
        var items = itemsState[0]
        var setItems = itemsState[1]

        var msgState = React.useState('')
        var msg = msgState[0]
        var setMsg = msgState[1]

        var busyState = React.useState(false)
        var busy = busyState[0]
        var setBusy = busyState[1]

        var llmState = React.useState(null)
        var llm = llmState[0]
        var setLlm = llmState[1]

        var modalState = React.useState(null)
        var modal = modalState[0]
        var setModal = modalState[1]

        // ── 自动更新（本插件自带能力，与其它插件零耦合）
        var updateInfoState = React.useState(null)
        var updateInfo = updateInfoState[0]
        var setUpdateInfo = updateInfoState[1]
        var updateBusyState = React.useState(null)
        var updateBusy = updateBusyState[0]
        var setUpdateBusy = updateBusyState[1]

        var call = function (payload) {
          return apiCall(payload, false)
        }

        var refresh = function () {
          setBusy(true)
          Promise.all([call({ op: 'list' }), call({ op: 'llm' })])
            .then(function (res) {
              if (res[0] && res[0].ok) setItems(res[0].items || [])
              if (res[1] && res[1].ok) setLlm(res[1])
              setMsg('')
            })
            .catch(function (error) {
              setMsg('读取失败：' + error)
            })
            .then(function () {
              setBusy(false)
            })
        }

        React.useEffect(function () {
          refresh()
        }, [])

        // 更新状态：挂载时查一次（走 Host 侧 6 小时缓存），离开时不再回调。
        React.useEffect(function () {
          var alive = true
          var settle = function (res) {
            if (!alive) return
            setUpdateInfo(res || null)
            setUpdateBusy(null)
          }
          setUpdateBusy('checking')
          call({ op: 'update_status', force: false }).then(settle, function () {
            if (alive) setUpdateBusy(null)
          })
          return function () {
            alive = false
          }
        }, [])

        var loadUpdate = function (force) {
          setUpdateBusy('checking')
          return call({ op: 'update_status', force: force === true })
            .then(function (res) {
              setUpdateInfo(res || null)
              setUpdateBusy(null)
              return res
            })
            .catch(function (error) {
              setUpdateBusy(null)
              setMsg('检查更新失败：' + error)
              return null
            })
        }

        var runUpdate = function () {
          setUpdateBusy('applying')
          return call({ op: 'update_apply' })
            .then(function (res) {
              setUpdateBusy(null)
              if (res && res.ok === true) {
                setMsg(
                  res.alreadyLatest === true
                    ? '已经是最新版本。'
                    : '已从 ' + (res.from || '—') + ' 升级到 ' + (res.to || '—') + '，重启 DSH 后生效。',
                )
              } else {
                setMsg('升级失败：' + ((res && (res.message || res.error)) || 'unknown'))
              }
              return loadUpdate(true)
            })
            .catch(function (error) {
              setUpdateBusy(null)
              setMsg('升级失败：' + error)
              return null
            })
        }

        var openRepo = function () {
          return call({ op: 'open_repo' })
            .then(function (res) {
              if (res && res.ok === true) return res
              setMsg('无法打开浏览器，请手动访问仓库页。')
              return res
            })
            .catch(function (error) {
              setMsg('无法打开浏览器：' + error)
              return null
            })
        }

        var openNew = function () {
          setModal({ id: null, text: '' })
        }
        var openEdit = function (item) {
          setModal({ id: item.id, text: item.content })
        }
        var closeModal = function () {
          setModal(null)
        }

        var saveModal = function () {
          if (!modal) return
          var text = modal.text.trim()
          if (text.length < 4) {
            setMsg('内容太短，多写几个字喵')
            return
          }
          setBusy(true)
          var payload = modal.id
            ? { op: 'edit_text', id: modal.id, text: text }
            : { op: 'add_text', text: text }
          call(payload)
            .then(function (res) {
              if (!res.ok) {
                setMsg('保存失败：' + (res.error || '未知错误'))
                return
              }
              setModal(null)
              if (res.results && res.results.length) {
                var first = res.results[0]
                setMsg('已保存：' + first.kind + ' / ' + first.predicate + ' = ' + first.object)
              } else {
                setMsg('已保存')
              }
              refresh()
            })
            .catch(function (error) {
              setMsg('保存失败：' + error)
            })
            .then(function () {
              setBusy(false)
            })
        }

        var removeRow = function (item) {
          if (!window.confirm('确定删除这条记忆吗？\n' + item.content)) return
          setBusy(true)
          call({ op: 'delete', id: item.id })
            .then(function (res) {
              setMsg(res.ok ? '已删除' : '删除失败：' + (res.error || ''))
              refresh()
            })
            .catch(function (error) {
              setMsg('删除失败：' + error)
            })
            .then(function () {
              setBusy(false)
            })
        }

        var badge = function (status) {
          return h('span', { className: 'dm-badge ' + status }, status)
        }

        var llmBlock = null
        if (llm === null) {
          llmBlock = h('div', { className: 'dm-hint' }, '读取中…')
        } else if (llm.configured) {
          llmBlock = h(
            'div',
            null,
            h(
              'div',
              { className: 'dm-llm-line' },
              h('span', null, h('span', { className: 'dm-code' }, llm.model), ' @ ', h('span', { className: 'dm-code' }, llm.base_url)),
              h('span', { className: 'dm-hint' }, 'key：' + llm.api_key_masked),
            ),
            h('div', { className: 'dm-hint' }, '类型、键、状态与置信度都由它判定；你只管写内容。'),
          )
        } else {
          llmBlock = h(
            'div',
            null,
            h('div', { className: 'dm-hint' }, '未配置。LLM 用来判定记忆的类型 / 键 / 重复 / 失效；没配置时走规则，解析会粗糙很多。'),
            h('pre', { className: 'dm-pre' }, 'memory-consolidation\\mem.cmd llm set --api-key sk-...'),
            h('div', { className: 'dm-hint' }, '默认端点 https://api.deepseek.com/v1 · 模型 deepseek-chat'),
          )
        }

        var dialog = null
        if (modal) {
          dialog = h(
            'div',
            {
              className: 'dm-overlay',
              onClick: function (event) {
                if (event.target === event.currentTarget) closeModal()
              },
            },
            h(
              'div',
              { className: 'dm-modal', role: 'dialog', 'aria-modal': 'true' },
              h('h3', null, modal.id ? '编辑记忆' : '新增记忆'),
              h('p', null, modal.id
                ? '改完之后由 LLM 重新判定类型、键与置信度；状态与失效链仍由巩固层决定。'
                : '用一句话写清楚这件事。类型、键、object、置信度都由 LLM 判定，你不用填。'),
              h('textarea', {
                className: 'dm-textarea',
                autoFocus: true,
                value: modal.text,
                placeholder: '例如：我做 Java 后端开发，日常用 IntelliJ IDEA',
                onChange: function (event) {
                  setModal({ id: modal.id, text: event.target.value })
                },
                onKeyDown: function (event) {
                  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') saveModal()
                  if (event.key === 'Escape') closeModal()
                },
              }),
              h('div', { className: 'dm-hint' }, 'Ctrl + Enter 保存 · Esc 取消'),
              h(
                'div',
                { className: 'dm-modal-foot' },
                h('button', { className: 'dm-btn', disabled: busy, onClick: closeModal }, '取消'),
                h('button', { className: 'dm-btn primary', disabled: busy, onClick: saveModal }, modal.id ? '保存修改' : '添加'),
              ),
            ),
          )
        }

        var list = items.length === 0
          ? h('div', { className: 'dm-empty' },
              h('div', { style: { fontSize: 22, marginBottom: 6 } }, '🐋'),
              '还没有记忆。点右上角「新增记忆」，或者在会话里说一句偏好。')
          : h(
              'div',
              { className: 'dm-list' },
              items.map(function (item, index) {
                // 迁移 002：优先显示 summary（人话），谓词只做脚注
                var text = item.summary || item.content || ''
                var where = ''
                if (item.scope === 'workspace') where = '本工作区'
                else if (item.scope === 'project') where = '项目'
                return h(
                  'div',
                  { className: 'dm-item' + (item.expired_by ? ' dm-item-expired' : ''), key: item.id || index },
                  h(
                    'div',
                    { className: 'dm-item-main' },
                    h('div', { className: 'dm-item-top' },
                      badge(item.status),
                      where ? h('span', { className: 'dm-tag' }, where) : null,
                      item.expired_by ? h('span', { className: 'dm-tag warn' }, '已失效：' + expiredLabel(item.expired_by)) : null,
                      h('span', { className: 'dm-content' }, text),
                    ),
                    h('div', { className: 'dm-meta' },
                      h('span', null, kindLabel(item.kind)),
                      h('span', null, '置信度 ' + Number(item.confidence).toFixed(2)),
                      h('span', null, '最近 ' + String(item.last_seen_at || '').slice(0, 10)),
                      // 原始键名折叠成一个可展开的 details，默认不显示
                      h('details', { className: 'dm-raw' },
                        h('summary', null, '原始键名'),
                        h('code', null, item.predicate + ' = ' + item.object),
                      ),
                    ),
                  ),
                  h('div', { className: 'dm-actions' },
                    h('button', { className: 'dm-btn tiny', onClick: function () { openEdit(item) } }, '编辑'),
                    h('button', { className: 'dm-btn tiny', onClick: function () { removeRow(item) } }, '删除'),
                  ),
                )
              }),
            )

        // ── 插件小图标（鲸鱼娘 Q 版）：和标题并排，尺寸跟随行高
        // ── 插件小图标（Q 版鲸鱼娘，内嵌位图）
        function PluginIcon(props) {
          var size = (props && typeof props.size === 'number') ? props.size : 28
          return h('img', {
            className: 'dm-plugin-icon',
            src: PLUGIN_ICON_DATA_URL,
            width: size,
            height: size,
            alt: '',
            'aria-hidden': 'true',
            draggable: 'false',
          })
        }
        // ── 自动更新卡片：版本三联 + 检查更新 + 立即升级 + 去 GitHub 点 Star
        //    数据全部来自本插件自己的 update_status / update_apply / open_repo，
        //    与 archive-manager 之类的其它插件没有任何耦合。
        function UpdateCard(props) {
          var info = props.info
          var busy = props.busy
          var runtimeVersion = info && info.runtimeVersion ? info.runtimeVersion : '—'
          var diskVersionText = info && info.diskVersion ? info.diskVersion : '—'
          var latestVersion = info && info.latestVersion ? info.latestVersion : null
          var canSelfUpdate = !!(info && info.canSelfUpdate === true)
          var updateAvailable = !!(info && info.updateAvailable === true)
          var restartRequired = !!(info && info.restartRequired === true)
          var canStar = !!(info && typeof info.htmlUrl === 'string' && info.htmlUrl !== '')

          var statusText
          if (busy === 'applying') statusText = '正在升级，请稍候…'
          else if (busy === 'checking') statusText = '正在检查更新…'
          else if (!info) statusText = '尚未检查'
          else if (restartRequired) statusText = '代码已更新，重启 DSH 后生效。'
          else if (updateAvailable) statusText = '可升级到 ' + latestVersion
          else if (info.error) statusText = '检查失败：' + info.error
          else if (info.message) statusText = info.message
          else statusText = '已经是最新版本。'

          var tone = (restartRequired || updateAvailable) ? 'warn' : ((info && info.error) ? 'error' : 'ok')
          var checkedText = (info && info.checkedAt) ? String(info.checkedAt).replace('T', ' ').slice(0, 16) : '尚未检查'

          return h(
            'div',
            { className: 'dm-update' },
            h(
              'div',
              { className: 'dm-update-head' },
              h('h3', { className: 'dm-update-title' }, '插件升级'),
              h('p', { className: 'dm-update-hint' },
                '更新方式取决于安装来源：本插件自带升级，直接从 GitHub 拉取新版本；由宿主管理的安装请通过原安装方式更新。'),
            ),
            h(
              'div',
              { className: 'dm-chips' },
              h('span', { className: 'dm-chip-group' }, h('span', { className: 'dm-chip-label' }, '运行版本'), h('span', { className: 'dm-chip' }, runtimeVersion)),
              h('span', { className: 'dm-chip-group' }, h('span', { className: 'dm-chip-label' }, '磁盘版本'), h('span', { className: 'dm-chip' }, diskVersionText)),
              h('span', { className: 'dm-chip-group' }, h('span', { className: 'dm-chip-label' }, '最新版本'), h('span', { className: 'dm-chip' }, latestVersion !== null ? latestVersion : '—')),
            ),
            h('div', { className: 'dm-update-status dm-update-status-' + tone }, statusText),
            h(
              'div',
              { className: 'dm-update-actions' },
              h('button', {
                className: 'dm-btn',
                disabled: !!busy,
                onClick: function () { props.onCheck() },
              }, busy === 'checking' ? '检查中…' : '检查更新'),
              h('button', {
                className: 'dm-btn primary',
                disabled: !!busy || !canSelfUpdate || (!updateAvailable && !restartRequired),
                title: canSelfUpdate ? '' : 'package.json 里还没有配置真实的仓库地址',
                onClick: function () { props.onApply() },
              }, busy === 'applying' ? '升级中…' : (restartRequired ? '去重启' : '立即升级')),
              canStar ? h('button', {
                className: 'dm-btn',
                disabled: !!busy,
                onClick: function () { props.onStar() },
              }, h('span', { className: 'dm-star-glyph', 'aria-hidden': 'true' }, '★'), ' 去 GitHub 点 Star') : null,
            ),
            h('div', { className: 'dm-update-foot' }, '上次检查：' + checkedText + ' · 自动检查：每 6 小时'),
          )
        }

        var body = h(
          'div',
          { className: 'dm-root' },
          h(
            'div',
            { className: 'dm-head' },
            h('h2', null, h(PluginIcon, null), '长期记忆'),
            h('button', { className: 'dm-btn primary', onClick: openNew }, '＋ 新增记忆'),
          ),
          h(UpdateCard, {
            info: updateInfo,
            busy: updateBusy,
            onCheck: function () { void loadUpdate(true) },
            onApply: function () { void runUpdate() },
            onStar: function () { void openRepo() },
          }),
          h('p', { className: 'dm-sub' },
            '跨会话自动调取。你只负责写下这件事本身，类型、键、置信度由 LLM 判定；修改会写审计日志并刷新注入快照。'),

          h(
            'div',
            { className: 'dm-card' },
            h(
              'div',
              { className: 'dm-card-head' },
              h('div', { className: 'dm-card-title' },
                h('span', { className: 'dm-dot ' + (llm && llm.configured ? 'on' : 'off') }),
                '巩固引擎（LLM）',
              ),
              h('span', { className: 'dm-hint' }, llm && llm.configured ? '已就绪' : '规则模式'),
            ),
            llmBlock,
          ),

          h(
            'div',
            { className: 'dm-card' },
            h(
              'div',
              { className: 'dm-card-head' },
              h('div', { className: 'dm-card-title' }, '全部记忆'),
              h('span', { className: 'dm-hint' }, items.length + ' 条' + (msg ? ' · ' + msg : '')),
            ),
            list,
          ),
        )

        // 弹窗挂到 body：设置面板祖先可能有 transform/contain，fixed 会失效
        if (dialog && ReactDOM && typeof ReactDOM.createPortal === 'function') {
          return h(React.Fragment, null, body, ReactDOM.createPortal(dialog, document.body))
        }
        return h(React.Fragment, null, body, dialog)
      }

      return Panel
    }

    exports.apply = function apply(ctx) {
      try {
        var React = require('react')
        if (!React || typeof React.createElement !== 'function') {
          console.warn('[dsh-tide-memory] 拿不到 react，设置页不注册')
          return
        }
        if (!ctx || typeof ctx.inject !== 'function') {
          console.warn('[dsh-tide-memory] 没有 ctx.inject，设置页不注册')
          return
        }
        var ReactDOM = null
        try {
          ReactDOM = require('react-dom')
        } catch (error) {
          console.warn('[dsh-tide-memory] 拿不到 react-dom，弹窗将退化为内联渲染')
        }

        injectStyle()
        var Panel = makePanel(React, ReactDOM)

        ctx.inject(['slots'], function (owner) {
          var slots = owner.get ? owner.get('slots') : owner.slots
          if (!slots || typeof slots.register !== 'function') {
            console.warn('[dsh-tide-memory] 拿不到 slots，设置页不注册')
            return
          }
          try {
            slots.inject('settings.section', function () {
              return slots.register(
                {
                  name: 'settings.section',
                  id: PKG,
                  order: 45,
                  label: function () {
                    return '记忆'
                  },
                },
                Panel,
              )
            })
            console.info('[dsh-tide-memory] 已注册到 settings.section')
          } catch (error) {
            console.warn('[dsh-tide-memory] 注册 settings.section 失败', error)
          }

          // 首次运行引导：只在「没有可用 Python」时出现，装好或点过「不再提醒」就永不打扰。
          try {
            var EnvGate = makeEnvGate(React)
            slots.inject('settings.onboarding', function () {
              return slots.register(
                {
                  name: 'settings.onboarding',
                  id: PKG + '-python-env',
                  order: 60,
                  label: function () {
                    return '记忆引擎环境'
                  },
                },
                EnvGate,
              )
            })
            console.info('[dsh-tide-memory] 已注册到 settings.onboarding')
          } catch (error) {
            console.warn('[dsh-tide-memory] 注册 settings.onboarding 失败', error)
          }
        })
      } catch (error) {
        console.warn('[dsh-tide-memory] 设置页接线失败', error)
      }
    }

    return module.exports
  },
})
