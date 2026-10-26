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
      '@media (prefers-reduced-motion:reduce){.dm-btn,.dm-overlay,.dm-modal{transition:none;animation:none}}',
    ].join('\n')

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

        var body = h(
          'div',
          { className: 'dm-root' },
          h(
            'div',
            { className: 'dm-head' },
            h('h2', null, '🐋 长期记忆'),
            h('button', { className: 'dm-btn primary', onClick: openNew }, '＋ 新增记忆'),
          ),
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
