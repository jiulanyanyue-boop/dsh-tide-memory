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
      // ── 工作区分组视图
      '.dm-groups{display:flex;flex-direction:column;gap:12px}',
      '.dm-group{border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.18));border-radius:12px;padding:10px 12px;background:var(--dsw-alias-button-floating-hover,rgba(128,128,128,.04))}',
      '.dm-group-head{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin:0 0 2px}',
      '.dm-group-title{font-weight:600;font-size:12.5px;display:flex;align-items:center;gap:6px;min-width:0;word-break:break-word}',
      '.dm-group-count{font-size:11.5px;color:var(--dsw-alias-label-tertiary,#9a9a9a);white-space:nowrap}',
      '.dm-group-body{margin-top:2px}',
      '.dm-group-empty{padding:9px 2px;font-size:12px;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
      '.dm-group-toggle{display:flex;align-items:center;gap:6px;width:100%;padding:0;margin:0;border:none;background:transparent;color:inherit;font:inherit;font-weight:600;font-size:12.5px;cursor:pointer;text-align:left}',
      '.dm-group-toggle:hover .dm-group-caret{opacity:1}',
      '.dm-group-caret{display:inline-block;width:12px;text-align:center;opacity:.6;transition:transform .12s}',
      '.dm-group-caret.open{transform:rotate(90deg)}',
      '.dm-subgroup{margin:6px 0 0;padding:6px 0 2px;border-top:1px dashed var(--dsw-alias-border-l2,rgba(128,128,128,.2))}',
      '.dm-subgroup:first-child{border-top:none;padding-top:0;margin-top:2px}',
      '.dm-subgroup-head{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:11.5px;color:var(--dsw-alias-label-secondary,#8b8b8b);margin-bottom:2px}',
      '.dm-subgroup-title{min-width:0;word-break:break-word}',
      '.dm-hintbar{padding:8px 11px;margin:0 0 10px;border-radius:9px;border-left:3px solid var(--dsw-alias-brand-primary,#4d6bfe);background:color-mix(in srgb,var(--dsw-alias-brand-primary,#4d6bfe) 10%,transparent);font-size:12px;line-height:1.55;color:var(--dsw-alias-label-secondary,#6b7280)}',
      // ── API Key 安全配置卡片
      '.dm-key{display:flex;flex-direction:column;gap:8px}',
      '.dm-key-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12.5px}',
      '.dm-key-label{font-size:12px;color:var(--dsw-alias-label-secondary,#8b8b8b)}',
      '.dm-key-actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap}',
      '.dm-key-input{flex:1 1 220px;min-width:0;box-sizing:border-box;height:32px;padding:0 10px;border-radius:9px;border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.3));background:transparent;color:inherit;font:inherit;outline:none;transition:border-color .15s,box-shadow .15s}',
      '.dm-key-input:focus{border-color:var(--dsw-alias-brand-primary,#4d6bfe);box-shadow:0 0 0 3px color-mix(in srgb,var(--dsw-alias-brand-primary,#4d6bfe) 16%,transparent)}',
      '.dm-key-input:disabled{opacity:.5;cursor:not-allowed}',
      '.dm-badge.key-ok{color:#16a34a;background:rgba(34,197,94,.12);border-color:rgba(34,197,94,.25)}',
      '.dm-badge.key-off{color:var(--dsw-alias-label-secondary,#8b8b8b);background:rgba(128,128,128,.12);border-color:rgba(128,128,128,.22)}',
      // ── 更新卡片：成功源 + 诊断
      '.dm-update-source{margin:0 0 8px;font-size:11.5px;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
      '.dm-update-diag{margin:0 0 10px;font-size:11.5px;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
      '.dm-update-diag summary{cursor:pointer;opacity:.85;list-style:none}',
      '.dm-update-diag summary::-webkit-details-marker{display:none}',
      '.dm-update-diag summary:hover{opacity:1;text-decoration:underline}',
      '.dm-update-diag ul{margin:5px 0 0;padding-left:16px;line-height:1.65}',
      '.dm-update-diag-src{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}',
      '.dm-update-diag-err{color:#dc2626}',
      // ── 引擎状态标识区 + 测试生成（任务 6 / 契约 §3b）
      '.dm-engine{display:flex;flex-direction:column;gap:8px;margin-top:11px;padding-top:11px;border-top:1px dashed var(--dsw-alias-border-l2,rgba(128,128,128,.22))}',
      '.dm-engine-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}',
      '.dm-engine-badge{display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:2px 10px;border-radius:999px;border:1px solid transparent;white-space:nowrap}',
      '.dm-engine-badge.on{color:#16a34a;background:rgba(34,197,94,.12);border-color:rgba(34,197,94,.28)}',
      '.dm-engine-badge.rule{color:#d97706;background:rgba(245,158,11,.12);border-color:rgba(245,158,11,.3)}',
      '.dm-engine-badge.unknown{color:var(--dsw-alias-label-secondary,#8b8b8b);background:rgba(128,128,128,.12);border-color:rgba(128,128,128,.24)}',
      '.dm-engine-reason{margin:0;font-size:12px;line-height:1.55;color:var(--dsw-alias-label-secondary,#8b8b8b)}',
      '.dm-engine-checks{margin:0;font-size:11.5px;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
      '.dm-engine-checks summary{cursor:pointer;list-style:none;opacity:.85}',
      '.dm-engine-checks summary::-webkit-details-marker{display:none}',
      '.dm-engine-checks summary:hover{opacity:1;text-decoration:underline}',
      '.dm-engine-checks ul{margin:6px 0 0;padding-left:16px;line-height:1.7}',
      '.dm-engine-check-ok{color:#16a34a}',
      '.dm-engine-check-off{color:#d97706}',
      '.dm-engine-val{font-size:11.5px;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
      '.dm-test{font-size:12.5px}',
      '.dm-test-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}',
      '.dm-test-out{padding:9px 11px;border-radius:10px;border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.18));background:var(--dsw-alias-button-floating-hover,rgba(128,128,128,.06))}',
      '.dm-test-degrade{margin:6px 0 0;font-size:12px;line-height:1.55;color:#d97706}',
      '.dm-test-cand{display:flex;flex-direction:column;gap:2px;padding:6px 0;border-bottom:1px dashed var(--dsw-alias-border-l2,rgba(128,128,128,.2))}',
      '.dm-test-cand:last-child{border-bottom:none}',
      '.dm-test-cand-key{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11.5px;color:var(--dsw-alias-label-secondary,#8b8b8b);word-break:break-word}',
      '.dm-test-cand-content{font-size:12px;line-height:1.55;word-break:break-word}',
      '.dm-test-note{margin:8px 0 0;font-size:11px;line-height:1.55;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
      '.dm-test-more{font-size:11.5px;color:var(--dsw-alias-label-tertiary,#9a9a9a);padding-top:4px}',
      '.dm-test-empty{font-size:12px;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
      /* ══ 页签条 + 开关（学 our-free-model 的 ofm_tabs / ofm_switch）════
       *
       * 为什么要页签：面板内容长成了一条竖直瀑布（升级卡片 → 引擎卡片 →
       * 记忆列表），用户要滚到最后才能看到记忆。ofm 用「页签条 + 每页一个
       * 组件」解决同类问题，这里照搬交互、样式用自己的 dm- 体系。
       *
       * 为什么要开关：autoConsolidate 这类「可选增强」原来只能改配置文件，
       * 用户根本不知道有这功能。做成 Switch，一点就切换并持久化。
       */
      '.dm-tabs{position:relative;display:flex;flex-wrap:wrap;gap:2px;padding:3px;border-radius:13px;border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.26));background:var(--dsw-alias-bg-elevated,var(--dsw-alias-button-elevated-fill,transparent));box-shadow:inset 0 1px 2px rgba(0,0,0,.04);margin:0 0 14px}',
      '.dm-tab{position:relative;z-index:1;font:inherit;font-size:12.5px;font-weight:600;padding:6px 14px;border-radius:10px;border:0;background:transparent;color:var(--dsw-alias-label-secondary,#8b8b8b);cursor:pointer;white-space:nowrap;transition:color .2s ease,background .2s ease}',
      '.dm-tab[aria-selected="true"]{color:var(--dsw-alias-label-primary,inherit);background:var(--dsw-alias-button-floating-hover,rgba(128,128,128,.14))}',
      '.dm-tab:hover{color:var(--dsw-alias-label-primary,inherit)}',
      '.dm-tab .dm-tabnum{font-variant-numeric:tabular-nums;font-weight:600;color:var(--dsw-alias-label-tertiary,#9a9a9a);margin-left:6px;font-size:11px}',
      /* Switch：role=switch 的按钮 + aria-checked，样式完全走主题变量 */
      '.dm-switch{display:inline-flex;align-items:center;gap:9px;cursor:pointer;user-select:none;-webkit-tap-highlight-color:transparent;appearance:none;background:transparent;border:0;padding:0;font:inherit;color:var(--dsw-alias-label-secondary,#8b8b8b);font-size:12.5px}',
      '.dm-switch i{position:relative;width:36px;height:20px;border-radius:999px;background:var(--dsw-alias-bg-layer-1,rgba(128,128,128,.18));border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.3));box-shadow:inset 0 1px 2px rgba(0,0,0,.06);transition:background .2s ease,border-color .2s ease;flex:none}',
      '.dm-switch i::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--dsw-alias-label-secondary,#8b8b8b);box-shadow:0 1px 2px rgba(0,0,0,.2);transition:transform .2s ease,background .2s ease}',
      '.dm-switch:hover i{border-color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
      '.dm-switch[aria-checked="true"] i{background:var(--dsw-alias-state-business-primary,#4d6bfe);border-color:color-mix(in srgb,var(--dsw-alias-state-business-primary,#4d6bfe) 85%,transparent)}',
      '.dm-switch[aria-checked="true"] i::after{transform:translateX(16px);background:#fff}',
      '.dm-switch:disabled{opacity:.5;cursor:not-allowed}',
      /* 设置卡：开关行 + 说明 */
      '.dm-settings{border:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.26));border-radius:14px;padding:14px 16px;margin-bottom:14px;background:var(--dsw-alias-button-elevated-fill,transparent);box-shadow:0 1px 2px rgba(0,0,0,.03)}',
      '.dm-settings-title{font-weight:600;font-size:13.5px;margin:0 0 10px;display:flex;align-items:center;gap:8px}',
      '.dm-setting-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.12));flex-wrap:wrap}',
      '.dm-setting-row:last-child{border-bottom:none}',
      '.dm-setting-name{font-size:13px;color:var(--dsw-alias-label-primary,inherit)}',
      '.dm-setting-hint{font-size:11.5px;color:var(--dsw-alias-label-tertiary,#9a9a9a);margin-top:2px;line-height:1.55;max-width:640px}',
      '@media (prefers-reduced-motion:reduce){.dm-btn,.dm-overlay,.dm-modal,.dm-group-caret{transition:none;animation:none}}',
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

    function makePanel(React, ReactDOM, services) {
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

      /* ── 当前工作区识别（全链路容错）──────────────────────────────────────
       *
       * 设置页可能在任何时机被挂载，uiWorkspace / sessions / workspaces 任一服务
       * 都可能拿不到（未注册、还没 ready、被裁剪）。这里所有探测都吞异常，
       * 拿不到就退回空字符串 → 调用方传 cwd: undefined → 后端只返回 global。
       * 绝不让设置页因为「认不出工作区」而白屏。
       */
      function safeService(services, name) {
        if (!name) return null
        /* ① 先从 apply 阶段缓存的可选服务里取 */
        try {
          if (services && typeof services.resolve === 'function') {
            var cached = services.resolve(name)
            if (cached !== undefined && cached !== null) return cached
          }
        } catch (error) {
          /* 继续往下试 */
        }
        /* ② 再试 owner 直取（兼容 owner.get / owner[name] 两种形状） */
        try {
          var owner = services && services.owner ? services.owner : null
          if (owner) {
            if (typeof owner.get === 'function') {
              var got = owner.get(name)
              if (got !== undefined && got !== null) return got
            }
            var direct = owner[name]
            if (direct !== undefined && direct !== null) return direct
          }
        } catch (error) {
          /* 服务未注册/取用失败：静默降级 */
        }
        return null
      }

      function snapshotOf(controller) {
        if (!controller) return null
        var target = controller.list && controller.list.getSnapshot ? controller.list : controller
        try {
          if (typeof target.getSnapshot === 'function') return target.getSnapshot()
        } catch (error) {
          return null
        }
        return null
      }

      /**
       * 取当前会话 cwd：uiWorkspace.selection → sessions.list.byId[id].cwd；
       * 选不到就退到「最近一个带 cwd 的会话」；仍然没有就返回 ''。
       */
      function resolveCwd(sessions, uiWorkspace) {
        var snapshot = snapshotOf(sessions)
        var byId = snapshot && snapshot.byId ? snapshot.byId : null
        if (!byId) return ''
        var sessionId = ''
        try {
          var selection = uiWorkspace ? uiWorkspace.selection : null
          var selSnap = selection && typeof selection.getSnapshot === 'function' ? selection.getSnapshot() : null
          if (selSnap && typeof selSnap.sessionId === 'string') sessionId = selSnap.sessionId
        } catch (error) {
          sessionId = ''
        }
        if (sessionId) {
          var picked = byId[sessionId]
          if (picked && typeof picked.cwd === 'string' && picked.cwd) return picked.cwd
        }
        var ids = []
        if (snapshot.ids && snapshot.ids.length) ids = snapshot.ids
        else {
          /* 没有 ids 数组时用对象键兜底（顺序不保证最新，但聊胜于无） */
          var key
          for (key in byId) {
            if (Object.prototype.hasOwnProperty.call(byId, key)) ids.push(key)
          }
        }
        var i
        for (i = 0; i < ids.length; i += 1) {
          var row = byId[ids[i]]
          if (row && typeof row.cwd === 'string' && row.cwd) return row.cwd
        }
        return ''
      }

      /**
       * 订阅 sessions.list / workspaces.list；变化时回调一次。
       * 返回一个总是可安全调用的取消函数。
       */
      function watchWorkspaceSignals(sessions, uiWorkspaces, onChange) {
        var offs = []
        var add = function (controller) {
          if (!controller) return
          var target = controller.list && typeof controller.list.subscribe === 'function' ? controller.list : controller
          if (typeof target.subscribe !== 'function') return
          try {
            var off = target.subscribe(function () {
              try {
                onChange()
              } catch (error) {
                /* 回调异常不传染给订阅方 */
              }
            })
            if (typeof off === 'function') offs.push(off)
          } catch (error) {
            /* 订阅失败：退回只读，不重试 */
          }
        }
        add(sessions)
        add(uiWorkspaces)
        return function () {
          var i
          for (i = 0; i < offs.length; i += 1) {
            var off = offs[i]
            try {
              if (typeof off === 'function') off()
            } catch (error) {
              /* 取消订阅失败忽略 */
            }
          }
          offs = []
        }
      }


      function Panel() {
        var itemsState = React.useState([])
        var items = itemsState[0]
        var setItems = itemsState[1]

        /* 后端 list 响应里的 current（工作区没识别到就是 null） */
        var currentState = React.useState(null)
        var current = currentState[0]
        var setCurrent = currentState[1]

        /* 当前会话 cwd（'' 表示没识别到） */
        var cwdState = React.useState('')
        var cwd = cwdState[0]
        var setCwd = cwdState[1]

        var msgState = React.useState('')
        var msg = msgState[0]
        var setMsg = msgState[1]

        var busyState = React.useState(false)
        var busy = busyState[0]
        var setBusy = busyState[1]

        var llmState = React.useState(null)
        var llm = llmState[0]
        var setLlm = llmState[1]

        /* 引擎状态（op:'engine_status'，契约 §3b）：null = 还没拿到（老后端 / 失败） */
        var engineState = React.useState(null)
        var engine = engineState[0]
        var setEngine = engineState[1]

        /* 测试生成（op:'engine_test'）：结果只读展示，绝不触发记忆列表刷新 */
        var testBusyState = React.useState(false)
        var testBusy = testBusyState[0]
        var setTestBusy = testBusyState[1]
        var testResultState = React.useState(null)
        var testResult = testResultState[0]
        var setTestResult = testResultState[1]
        var testErrorState = React.useState('')
        var testError = testErrorState[0]
        var setTestError = testErrorState[1]

        /* API Key 输入框的值只活在本地 state，永远不回填服务端值 */
        var keyInputState = React.useState('')
        var keyInput = keyInputState[0]
        var setKeyInput = keyInputState[1]

        var baseUrlInputState = React.useState('')
        var baseUrlInput = baseUrlInputState[0]
        var setBaseUrlInput = baseUrlInputState[1]

        var modelInputState = React.useState('')
        var modelInput = modelInputState[0]
        var setModelInput = modelInputState[1]

        var keyBusyState = React.useState(false)
        var keyBusy = keyBusyState[0]
        var setKeyBusy = keyBusyState[1]

        var modalState = React.useState(null)
        var modal = modalState[0]
        var setModal = modalState[1]

        /* 「其它工作区」折叠组：默认展开，默认值 true */
        var otherOpenState = React.useState(true)
        var otherOpen = otherOpenState[0]
        var setOtherOpen = otherOpenState[1]
        /* 子分组折叠表：{ [scope_ref]: true } 表示收起 */
        var foldedState = React.useState({})
        var folded = foldedState[0]
        var setFolded = foldedState[1]

        /* ── 页签（学 our-free-model 的 ofm_tabs）────────────────────────
         * memory   = 记忆列表（默认页：这是用户最常看的）
         * engine   = 巩固引擎（LLM 配置 + 引擎状态 + 测试生成）
         * settings = 通用设置（自动整理开关等）
         * upgrade  = 插件升级
         */
        var tabState = React.useState('memory')
        var tab = tabState[0]
        var setTab = tabState[1]

        /* ── 通用设置（op:'settings_get' / 'settings_set'）───────────────
         * null = 还没拿到（老后端没有这个 op）；拿到后是个 { key: value } 平面表 */
        var settingsState = React.useState(null)
        var settings = settingsState[0]
        var setSettings = settingsState[1]
        var settingsBusyState = React.useState(false)
        var settingsBusy = settingsBusyState[0]
        var setSettingsBusy = settingsBusyState[1]

        /* 每日整理时刻输入框：只在本地暂存，点「保存时刻」才提交 */
        var dailyTimeInputState = React.useState('')
        var dailyTimeInput = dailyTimeInputState[0]
        var setDailyTimeInput = dailyTimeInputState[1]
        var dailyTimeDirty = React.useRef(false)

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

        /* 当前 cwd 在闭包里随时会变，用 ref 避免 refresh 被旧值钉住 */
        var cwdRef = React.useRef('')
        cwdRef.current = cwd

        var refreshWith = function (useCwd) {
          setBusy(true)
          var listPayload = { op: 'list' }
          if (useCwd) listPayload.cwd = useCwd
          /* workspaces 是新增 op，后端还没上线时可能 404/ok:false；
           * 单独兜住，绝不让它把 list 一起拖垮。 */
          var workspacesProbe = call({ op: 'workspaces' }).catch(function () {
            return null
          })
          /* engine_status 同样是新增 op：老后端会 404 / ok:false / 非 JSON，
           * 单独兜住，拿不到就当「引擎状态未知」，绝不拖垮 list 与 llm。 */
          var engineProbe = call({ op: 'engine_status' }).catch(function () {
            return null
          })
          /* 通用设置同样是新增 op：拿不到就保持 null（UI 显示「未知」并降级） */
          var settingsProbe = call({ op: 'settings_get' }).catch(function () {
            return null
          })
          return Promise.all([call(listPayload), call({ op: 'llm' }), workspacesProbe, engineProbe, settingsProbe])
            .then(function (res) {
              var listed = res[0]
              if (listed && listed.ok) {
                setItems(listed.items || [])
                /* current 可能不存在（旧后端）：当 null 处理，不报错 */
                setCurrent(listed.current || null)
              }
              if (res[1] && res[1].ok) setLlm(res[1])
              if (res[3] && res[3].ok === true) setEngine(res[3])
              else setEngine(null)
              if (res[4] && res[4].ok === true && res[4].settings && typeof res[4].settings === 'object') {
                setSettings(res[4].settings)
              } else {
                setSettings(null)
              }
              setMsg('')
            })
            .catch(function (error) {
              setMsg('读取失败：' + error)
            })
            .then(function () {
              setBusy(false)
            })
        }

        var refresh = function () {
          return refreshWith(cwdRef.current || '')
        }

        /* ── 挂载时：先认工作区，再拉数据；并订阅会话/工作区变化 ─────────────
         *
         * 任何一步失败都只是丢掉 cwd，不会中断面板渲染。
         * 服务可能比面板晚就绪（可选 inject 回调是异步的），所以这里做
         * 有限次重试：拿到 sessions 后补订阅，拿不到就安安静静当没有。
         */
        React.useEffect(function () {
          var alive = true
          var offSignals = null
          var tries = 0
          var retryTimer = null

          var scan = function () {
            var sessions = safeService(services, 'sessions')
            var uiWorkspace = safeService(services, 'uiWorkspace')
            var uiWorkspaces = safeService(services, 'workspaces')
            return {
              sessions: sessions,
              uiWorkspace: uiWorkspace,
              uiWorkspaces: uiWorkspaces,
            }
          }

          var pick = function (found) {
            var next = ''
            try {
              next = resolveCwd(found.sessions, found.uiWorkspace)
            } catch (error) {
              next = ''
            }
            return next
          }

          var bind = function (found) {
            if (offSignals) return
            offSignals = watchWorkspaceSignals(found.sessions, found.uiWorkspaces, function () {
              if (!alive) return
              var reread = scan()
              var next = pick(reread)
              if (next === cwdRef.current) return
              cwdRef.current = next
              setCwd(next)
              refreshWith(next)
            })
          }

          var found = scan()
          var initial = pick(found)
          cwdRef.current = initial
          setCwd(initial)
          refreshWith(initial)
          bind(found)

          /* 只有「服务还没到位」才值得重试；到位了就不再折腾 */
          var tick = function () {
            if (!alive) return
            tries += 1
            var again = scan()
            if (again.sessions || again.uiWorkspace) {
              bind(again)
              var next = pick(again)
              if (next !== cwdRef.current) {
                cwdRef.current = next
                setCwd(next)
                refreshWith(next)
              }
            }
            if (tries < 10 && !(again.sessions && again.uiWorkspace)) {
              retryTimer = setTimeout(tick, 400)
            }
          }
          retryTimer = setTimeout(tick, 400)

          return function () {
            alive = false
            if (retryTimer) clearTimeout(retryTimer)
            if (offSignals) offSignals()
          }
        }, [])

        /* 后端 llm 响应到了之后，把 base_url / model 预填进本地输入框，方便只改一项 */
        React.useEffect(function () {
          if (!llm) return
          setBaseUrlInput(typeof llm.base_url === 'string' ? llm.base_url : '')
          setModelInput(typeof llm.model === 'string' ? llm.model : '')
        }, [llm && llm.base_url, llm && llm.model])

        /* 后端 settings 到了之后，把每日整理时刻预填进输入框。
         * 只在「用户还没改过」时填（dailyTimeDirty），避免把用户正在输入的内容覆盖掉。 */
        React.useEffect(function () {
          if (!settings || dailyTimeDirty.current) return
          setDailyTimeInput(typeof settings.dailyConsolidateTime === 'string' ? settings.dailyConsolidateTime : '03:00')
        }, [settings && settings.dailyConsolidateTime])

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

        /* ── API Key 动作（任务 3）──────────────────────────────────────────
         *
         * 铁律：input 的 value 只存在本地 state；保存成功后立即清空，
         * 绝不用服务端返回的任何值回填 input。面板只显示 masked。
         */
        /* 双形态归一（契约 §3 v2）：llm / llm-set / llm-clear 同时回
         * 顶层扁平字段（stored/scheme/api_key_masked/secret_path…）与嵌套
         * `key` 对象。老后端只有扁平、未来也只有嵌套都能读。
         * 铁律：任一侧给出真值就采信；读不到方案也绝不误报「未存储」。 */
        var firstString = function () {
          var n
          for (n = 0; n < arguments.length; n += 1) {
            var v = arguments[n]
            if (typeof v === 'string' && v !== '') return v
          }
          return ''
        }
        var keyShapeOf = function (src) {
          if (!src || typeof src !== 'object') return null
          var nested = src.key && typeof src.key === 'object' ? src.key : null
          var out = {
            scheme: firstString(
              nested && nested.scheme,
              src.scheme,
              nested && nested.available_scheme,
              src.available_scheme,
              '',
            ),
            available_scheme: firstString(
              nested && nested.available_scheme,
              src.available_scheme,
              '',
            ),
            masked: firstString(src.api_key_masked, nested && nested.api_key_masked, ''),
            path: firstString(nested && nested.path, src.secret_path, ''),
            writable: null,
            decryptable: null,
            stored: false,
          }
          if (nested && typeof nested.writable === 'boolean') out.writable = nested.writable
          else if (typeof src.writable === 'boolean') out.writable = src.writable
          if (nested && typeof nested.decryptable === 'boolean') out.decryptable = nested.decryptable
          else if (typeof src.decryptable === 'boolean') out.decryptable = src.decryptable

          var flags = []
          if (nested && typeof nested.stored === 'boolean') flags.push(nested.stored)
          if (typeof src.stored === 'boolean') flags.push(src.stored)
          if (flags.length) {
            /* 两侧都给时取「或」：宁可少报一次错，也不要因为一侧缺失就说没存 */
            out.stored = flags.indexOf(true) >= 0
          } else if (out.scheme !== '' && out.scheme !== 'none') {
            out.stored = true
          } else if (out.masked !== '') {
            /* 有掩码但没报方案：密文确实存在 */
            out.stored = true
          } else if (src.configured === true) {
            /* 只有 configured 这一个信号：保守认为已配置，别亮红 */
            out.stored = true
          }
          return out
        }

        var keySchemeLabel = function (scheme, stored) {
          if (scheme === 'dpapi') return 'DPAPI 已加密'
          if (scheme === 'aesgcm-file') return '本机文件加密'
          if (scheme === 'none') return '未存储'
          /* 方案字段缺失但密钥确实存着 → 「已存储（方案未知）」，绝不写「未存储」 */
          if (stored === true) return '已存储（方案未知）'
          return scheme ? String(scheme) : '未存储'
        }
        var keySchemeTone = function (scheme, stored) {
          if (scheme === 'dpapi' || scheme === 'aesgcm-file') return 'key-ok'
          return stored === true ? 'key-ok' : 'key-off'
        }

        var applyKeyResult = function (res, keyChanged) {
          /* llm-set / llm-clear 都回 {ok, api_key_masked?, key?}（契约 §3 v2：
           * 扁平 + 嵌套双形态）。缺字段时用本地兜底，不覆盖已有信息。
           * 注意：只改 base_url / model 时不能用「配置成功」推出「已配置 LLM」，
           * 否则卡片会亮绿灯显示「已就绪」而实际没有密钥。 */
          if (!res || res.ok !== true) {
            setMsg('密钥操作失败：' + ((res && (res.error || res.message)) || '未知错误'))
            return false
          }
          var incoming = keyShapeOf(res)
          /* 响应里到底有没有密钥信息？没有（例如只存 base_url/model）就别乱动灯 */
          var hasKeyDetail = !!(
            res.key && typeof res.key === 'object'
          ) || typeof res.stored === 'boolean'
            || typeof res.scheme === 'string'
            || typeof res.api_key_masked === 'string'
          var storedKnown = !!(
            (res.key && typeof res.key.stored === 'boolean') || typeof res.stored === 'boolean'
          )
          setLlm(function (prev) {
            var base = prev || {}
            var next = {
              ok: true,
              configured: base.configured === true,
              base_url: base.base_url,
              model: base.model,
              api_key_masked: base.api_key_masked,
              key: base.key,
              config_path: base.config_path,
            }
            if (hasKeyDetail && incoming) {
              /* 统一补齐成嵌套形态给界面读，扁平值也照样能进来 */
              next.key = {
                stored: incoming.stored === true,
                scheme: incoming.scheme || 'none',
                available_scheme: incoming.available_scheme || '',
                path: incoming.path || '',
                writable: incoming.writable,
                decryptable: incoming.decryptable,
              }
              if (typeof res.api_key_masked === 'string') next.api_key_masked = res.api_key_masked
            }
            if (typeof res.base_url === 'string') next.base_url = res.base_url
            if (typeof res.model === 'string') next.model = res.model
            if (typeof next.api_key_masked !== 'string') next.api_key_masked = ''
            /* 只有明确知道密钥在不在，才动 configured 这个灯 */
            if (storedKnown) next.configured = incoming.stored === true
            else if (keyChanged === true) next.configured = true
            return next
          })
          return true
        }

        var saveKey = function () {
          var raw = (keyInput || '').trim()
          if (!raw) {
            setMsg('请先填入 API Key')
            return
          }
          setKeyBusy(true)
          call({ op: 'llm-set', api_key: raw })
            .then(function (res) {
              if (applyKeyResult(res, true)) setMsg('密钥已保存（加密落盘，面板只回显掩码）')
              /* 无论成功与否，输入框立刻清空：不让明文在界面上多留一秒 */
              setKeyInput('')
            })
            .catch(function (error) {
              setKeyInput('')
              setMsg('保存密钥失败：' + error)
            })
            .then(function () {
              setKeyBusy(false)
            })
        }

        var clearKey = function () {
          if (!window.confirm('确定清除已保存的 API Key 吗？\n清除后巩固引擎会退回规则模式。')) return
          setKeyBusy(true)
          call({ op: 'llm-clear' })
            .then(function (res) {
              if (!res || res.ok !== true) {
                setMsg('清除失败：' + ((res && (res.error || res.message)) || '未知错误'))
                return
              }
              setLlm(function (prev) {
                var base = prev || {}
                return {
                  ok: true,
                  configured: false,
                  base_url: base.base_url,
                  model: base.model,
                  api_key_masked: '',
                  key: res.key && typeof res.key === 'object' ? res.key : { stored: false, scheme: 'none' },
                  config_path: base.config_path,
                }
              })
              setKeyInput('')
              setMsg('密钥已清除')
            })
            .catch(function (error) {
              setMsg('清除密钥失败：' + error)
            })
            .then(function () {
              setKeyBusy(false)
            })
        }

        var saveEndpoint = function () {
          var payload = { op: 'llm-set' }
          if (typeof baseUrlInput === 'string' && baseUrlInput.trim()) payload.base_url = baseUrlInput.trim()
          if (typeof modelInput === 'string' && modelInput.trim()) payload.model = modelInput.trim()
          setKeyBusy(true)
          call(payload)
            .then(function (res) {
              if (applyKeyResult(res)) setMsg('端点与模型已保存')
            })
            .catch(function (error) {
              setMsg('保存配置失败：' + error)
            })
            .then(function () {
              setKeyBusy(false)
            })
        }

        /* ── 测试生成（契约 §3b）────────────────────────────────────────────
         *
         * 跑一次完整抽取 + 巩固（LLM 模式下真调），写操作全在后端临时库，
         * 真实记忆一个字节都不动。前端这里只读展示结果：
         * 不 refresh 列表、不动 cwd、不改任何其它 state，避免「测一下」污染面板。
         * 结果区在测试前就占好位置，主人随时知道按钮会做什么。
         */
        var runEngineTest = function () {
          if (testBusy) return
          setTestBusy(true)
          setTestError('')
          var payload = { op: 'engine_test' }
          /* cwd 用面板已有的值；拿不到就不传，让后端用内置探针句 + 全局作用域 */
          if (cwdRef.current) payload.cwd = cwdRef.current
          return call(payload)
            .then(function (res) {
              if (!res || res.ok !== true) {
                setTestResult(null)
                setTestError('测试失败：' + ((res && (res.error || res.message)) || '引擎未响应（该 op 可能还没上线）'))
                return null
              }
              setTestResult(res)
              return res
            })
            .catch(function (error) {
              setTestResult(null)
              setTestError('测试失败：' + error)
              return null
            })
            .then(function () {
              setTestBusy(false)
            })
        }

        /* ── 通用设置：读改走 settings_get / settings_set ─────────────────
         *
         * 可选增强以前只能改 $DSH_HOME 的配置文件——用户根本不知道有这些功能。
         * 现在一颗 Switch 就地切换，保存立即生效（后端写文件 + 热读）。
         * 后端没上线时按钮禁用并注明原因，绝不假装保存成功。
         */
        var saveSetting = function (key, value) {
          if (settingsBusy) return
          setSettingsBusy(true)
          /* 乐观更新：先把界面切过去，失败再弹回来——开关类交互不该有迟滞 */
          var prev = settings
          setSettings(function (s) {
            var next = Object.assign({}, s || {})
            next[key] = value
            return next
          })
          /* ⚠️ 补丁对象必须用「变量的值」做属性名。
           * 早期写成了字面量 { key: value }——发出去的键名就是字符串 "key"，
           * 后端白名单里没有这个键 → 被静默忽略 → 返回未改动的设置 →
           * 前端 setSettings(旧值) 把开关弹回原状。就是「点了又弹回去」的根因。 */
          var patch = {}
          patch[key] = value
          return call({ op: 'settings_set', settings: patch })
            .then(function (res) {
              if (!res || res.ok !== true) {
                setSettings(prev)
                setMsg('保存设置失败：' + ((res && (res.error || res.message)) || '该功能可能还没上线'))
                return null
              }
              /* 后端拒绝了某些键（白名单外/格式非法）时必须如实提示——
               * 否则用户以为保存了，实际没生效，比直接报错更坑。 */
              if (Array.isArray(res.rejected) && res.rejected.length > 0) {
                setSettings(prev)
                setMsg('以下设置项被拒绝（键名不合法或不在白名单）：' + res.rejected.join('、'))
                return null
              }
              /* 后端回传规范后的值（可能带类型修正），以后端为准 */
              if (res.settings && typeof res.settings === 'object') setSettings(res.settings)
              setMsg('')
              return res
            })
            .catch(function (error) {
              setSettings(prev)
              setMsg('保存设置失败：' + error)
              return null
            })
            .then(function (result) {
              /* 保存成功后解除「正在输入」标记，让后端值可以再次同步到输入框 */
              if (result && result.ok === true) dailyTimeDirty.current = false
              setSettingsBusy(false)
            })
        }

        /* 每日整理时刻的提交：校验 HH:MM 后送后端；非法就地提示 */
        var commitDailyTime = function () {
          var value = (dailyTimeInput || '').trim()
          if (!value) {
            setMsg('请先填写时刻（HH:MM）')
            return null
          }
          if (!/^([01]?\d|2[0-3]):([0-5]\d)$/.test(value)) {
            setMsg('时刻格式不对：请用 24 小时制 HH:MM，例如 03:00')
            return null
          }
          return saveSetting('dailyConsolidateTime', value)
        }

        var Switch = function (props) {
          return h('button', {
            type: 'button',
            className: 'dm-switch',
            role: 'switch',
            'aria-checked': props.checked ? 'true' : 'false',
            disabled: props.disabled === true,
            onClick: props.onChange,
          }, h('i'), props.label === undefined ? null : h('span', null, props.label))
        }

        /* ── 巩固引擎（LLM）卡片：掩码回显 + 密文方案徽标 + 明文输入框 ──────
         *
         * llm === null 表示还在读取：仍然渲染输入框（主人可能想直接贴 key），
         * 只是把「当前密钥」一行换成读取中的提示。绝不整块消失。
         */
        var llmBlock = null
        {
          var llmData = llm && typeof llm === 'object' ? llm : null
          /* 双形态归一：嵌套 key 优先，老后端的扁平字段照样能组装出等价对象。
           * 后端还没上线 / 字段缺失时，masked 与 configured 也能推出「已存储」。 */
          var keyShape = llmData ? keyShapeOf(llmData) : null
          var scheme = keyShape ? keyShape.scheme : ''
          var masked = keyShape ? keyShape.masked : ''
          var stored = keyShape ? keyShape.stored === true : false

          llmBlock = h(
            'div',
            { className: 'dm-key' },
            h('div', { className: 'dm-key-row' },
              h('span', { className: 'dm-key-label' }, '当前密钥'),
              llmData === null
                ? h('span', { className: 'dm-hint' }, '读取中…')
                : (stored
                    ? h('span', { className: 'dm-code' }, masked || '（已隐藏）')
                    : h('span', { className: 'dm-key-label' }, '未保存')),
              llmData === null
                ? null
                : h('span', { className: 'dm-badge ' + keySchemeTone(scheme, stored) }, keySchemeLabel(scheme, stored)),
            ),

            h('div', { className: 'dm-key-row' },
              h('input', {
                className: 'dm-key-input',
                type: 'text',
                autoComplete: 'off',
                spellCheck: false,
                placeholder: 'sk-...',
                value: keyInput,
                disabled: keyBusy,
                onChange: function (event) { setKeyInput(event.target.value) },
                onKeyDown: function (event) {
                  if (event.key === 'Enter') saveKey()
                },
              }),
              h('div', { className: 'dm-key-actions' },
                h('button', {
                  className: 'dm-btn primary tiny',
                  disabled: keyBusy || !(keyInput || '').trim(),
                  onClick: saveKey,
                }, keyBusy ? '保存中…' : '保存密钥'),
                h('button', {
                  className: 'dm-btn tiny',
                  disabled: keyBusy || !stored,
                  onClick: clearKey,
                }, '清除密钥'),
              ),
            ),
            h('div', { className: 'dm-hint' },
              '密钥用 Windows DPAPI 绑定当前用户加密存在本机（llm.secret），只在保存这一刻经手一次；',
              '保存后输入框立即清空，面板永远只回显掩码。'),

            h('div', { className: 'dm-key-row' },
              h('span', { className: 'dm-key-label' }, '端点'),
              h('input', {
                className: 'dm-key-input',
                type: 'text',
                autoComplete: 'off',
                placeholder: 'https://api.deepseek.com/v1',
                value: baseUrlInput,
                disabled: keyBusy,
                onChange: function (event) { setBaseUrlInput(event.target.value) },
              }),
            ),
            h('div', { className: 'dm-key-row' },
              h('span', { className: 'dm-key-label' }, '模型'),
              h('input', {
                className: 'dm-key-input',
                type: 'text',
                autoComplete: 'off',
                placeholder: 'deepseek-chat',
                value: modelInput,
                disabled: keyBusy,
                onChange: function (event) { setModelInput(event.target.value) },
              }),
              h('div', { className: 'dm-key-actions' },
                h('button', {
                  className: 'dm-btn tiny',
                  disabled: keyBusy,
                  onClick: saveEndpoint,
                }, '保存配置'),
              ),
            ),

            stored || (llmData && llmData.configured)
              ? h('div', { className: 'dm-hint' }, '类型、键、状态与置信度都由它判定；你只管写内容。')
              : h('div', { className: 'dm-hint' }, '未配置时走规则判定，解析会粗糙很多；填上密钥即可切到 LLM。'),
          )
        }

        /* ── 引擎状态标识区 + 测试生成（任务 6 / 契约 §3b）───────────────────
         *
         * engine 为 null（老后端没有这个 op、或读取失败）→ 显示「引擎状态未知」，
         * 卡片照常渲染，绝不白屏。engine_test 同理：失败只写一行错误文案，
         * 结果区永远占好位置，主人随时知道按钮会做什么。
         */
        var engineModeOf = function (mode) {
          return mode === 'llm' || mode === 'rule' ? mode : ''
        }
        var engineLabelOf = function (mode) {
          if (mode === 'llm') return 'LLM 生成模式'
          if (mode === 'rule') return '规则引擎模式'
          return '引擎状态未知'
        }
        var engineToneOf = function (mode) {
          if (mode === 'llm') return 'on'
          if (mode === 'rule') return 'rule'
          return 'unknown'
        }

        var engineRow = null
        var engineChecks = []
        var engineReason = ''
        if (engine && typeof engine === 'object') {
          var engineMode = engineModeOf(typeof engine.engine === 'string' ? engine.engine : '')
          var engineText = engineMode
            ? engineLabelOf(engineMode)
            : (typeof engine.engine_label === 'string' && engine.engine_label ? engine.engine_label : '引擎状态未知')
          if (typeof engine.reason === 'string') engineReason = engine.reason
          if (engine.checks && engine.checks.length) engineChecks = engine.checks
          engineRow = h('span', { className: 'dm-engine-badge ' + (engineMode ? engineToneOf(engineMode) : 'unknown') }, engineText)
        } else {
          engineRow = h('span', { className: 'dm-engine-badge unknown' }, '引擎状态未知')
        }

        var testArea = null
        if (testError) {
          testArea = h('div', { className: 'dm-test' },
            h('div', { className: 'dm-test-out' },
              h('div', { className: 'dm-test-empty' }, testError),
            ),
          )
        } else if (!testResult) {
          testArea = h('div', { className: 'dm-test' },
            h('div', { className: 'dm-test-note' },
              '点「测试生成」会跑一次完整的抽取 + 巩固流程（LLM 模式下真实调用 API），',
              '所有写操作落在临时数据库，不留下任何真实记忆。'),
          )
        } else {
          var tr = testResult
          var trMode = engineModeOf(typeof tr.engine === 'string' ? tr.engine : '')
          var trText = trMode
            ? engineLabelOf(trMode)
            : (typeof tr.engine_label === 'string' && tr.engine_label ? tr.engine_label : '引擎状态未知')
          var trHead = [
            h('span', {
              className: 'dm-engine-badge ' + (tr.degraded === true ? 'rule' : engineToneOf(trMode)),
              key: 'mode',
            }, trText),
          ]
          if (tr.elapsed_ms !== undefined && tr.elapsed_ms !== null) {
            trHead.push(h('span', { className: 'dm-engine-val', key: 'ms' }, String(tr.elapsed_ms) + ' 毫秒'))
          }
          var trCount = tr.candidate_count !== undefined && tr.candidate_count !== null
            ? tr.candidate_count
            : (tr.candidates && tr.candidates.length ? tr.candidates.length : 0)
          trHead.push(h('span', { className: 'dm-engine-val', key: 'count' }, '抽出候选 ' + trCount + ' 条'))

          var trRows = tr.candidates && tr.candidates.length ? tr.candidates : []
          var trBodies = []
          var ti
          for (ti = 0; ti < trRows.length && ti < 5; ti += 1) {
            var cand = trRows[ti] || {}
            trBodies.push(h('div', { className: 'dm-test-cand', key: 'c' + ti },
              h('div', { className: 'dm-test-cand-key' },
                (kindLabel(cand.kind) || '') + ' · ' + (cand.predicate || '') + ' = ' + (cand.object || '')),
              h('div', { className: 'dm-test-cand-content' }, cand.summary || cand.content || ''),
            ))
          }

          testArea = h('div', { className: 'dm-test' },
            h('div', { className: 'dm-test-out' },
              h('div', { className: 'dm-test-head' }, trHead),
              tr.degraded === true
                ? h('p', { className: 'dm-test-degrade' },
                    '已降级：' + (typeof tr.degrade_reason === 'string' && tr.degrade_reason ? tr.degrade_reason : '原因未知'))
                : null,
              trRows.length
                ? h('div', null, trBodies)
                : h('div', { className: 'dm-test-empty' }, '没有抽出候选：这条探针句没被判定成可复用记忆。'),
              trRows.length > 5
                ? h('div', { className: 'dm-test-more' }, '（还有 ' + (trRows.length - 5) + ' 条候选未显示）')
                : null,
              h('div', { className: 'dm-test-note' },
                typeof tr.note === 'string' && tr.note ? tr.note : '本测试在临时数据库执行，未写入任何真实记忆。'),
            ),
          )
        }

        var engineBlock = h('div', { className: 'dm-engine' },
          h('div', { className: 'dm-engine-row' },
            engineRow,
            h('button', {
              className: 'dm-btn tiny',
              disabled: testBusy,
              title: '跑一次完整生成流程，结果只读展示，不写入任何真实记忆',
              onClick: runEngineTest,
            }, testBusy ? '测试中…' : '测试生成'),
          ),
          engineReason ? h('p', { className: 'dm-engine-reason' }, engineReason) : null,
          engineChecks.length
            ? h('details', { className: 'dm-engine-checks' },
                h('summary', null, '判定细节（' + engineChecks.length + ' 项）'),
                h('ul', null,
                  engineChecks.map(function (check, index) {
                    var row = check || {}
                    return h('li', { key: index },
                      h('span', { className: row.ok === true ? 'dm-engine-check-ok' : 'dm-engine-check-off' },
                        row.ok === true ? '✅' : '⚠️'),
                      ' ' + (row.name || ('检查 ' + (index + 1))),
                      row.detail ? h('span', { className: 'dm-engine-val' }, ' · ' + row.detail) : null,
                    )
                  }),
                ),
              )
            : null,
          testArea,
        )

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

        /* ── 单条记忆（分组内复用）───────────────────────────────────────── */
        var renderItem = function (item, index) {
          // 迁移 002：优先显示 summary（人话），谓词只做脚注
          var text = item.summary || item.content || ''
          // 新契约：scope 标签用人话（workspace_title），不再只写「本工作区」
          var where = ''
          if (item.scope === 'workspace' || item.scope === 'project') {
            where = item.workspace_title || ''
          }
          if (!where && item.scope === 'workspace') where = '已登记工作区'
          if (!where && item.scope === 'project') where = '项目'
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
        }

        var renderItemList = function (rows, prefix) {
          return h(
            'div',
            { className: 'dm-list' },
            rows.map(function (item, index) {
              return renderItem(item, prefix + '-' + index)
            }),
          )
        }

        /* ── 按契约 §5 分组：当前工作区 → 其它工作区（可按 ref 折叠）→ 全局 ──
         *
         * current 由后端给出；老后端没有 current 字段时 current=null，
         * 此时全部 workspace/project 条目都算「其它工作区」，global 照常单独成组。
         * 分不出组的条目一律塞进「其它工作区」的「未标注」子组，保证不丢数据。
         */
        var currentRef = current && typeof current.workspace_ref === 'string' ? current.workspace_ref : ''
        var currentTitle = (current && (current.title || current.path)) || ''

        var globalItems = []
        var currentItems = []
        var otherGroups = [] // [{ ref, title, rows }]
        var newItem = function (id) {
          return { ref: id, title: '', rows: [] }
        }
        var otherIndex = {}
        var i
        for (i = 0; i < items.length; i += 1) {
          var one = items[i] || {}
          if (one.scope === 'global') {
            globalItems.push(one)
            continue
          }
          var ref = typeof one.scope_ref === 'string' ? one.scope_ref : ''
          if (ref && ref === currentRef) {
            currentItems.push(one)
            continue
          }
          var groupKey = ref || '__unscoped__'
          if (!Object.prototype.hasOwnProperty.call(otherIndex, groupKey)) {
            otherIndex[groupKey] = otherGroups.length
            var made = newItem(groupKey)
            made.title = one.workspace_title || (ref ? ref : '未标注工作区')
            otherGroups.push(made)
          }
          otherGroups[otherIndex[groupKey]].rows.push(one)
        }

        var list = null
        if (items.length === 0) {
          list = h('div', { className: 'dm-empty' },
            h('div', { style: { fontSize: 22, marginBottom: 6 } }, '🐋'),
            '还没有记忆。点右上角「新增记忆」，或者在会话里说一句偏好。')
        } else {
          var groups = []

          // ① 当前工作区
          if (currentRef) {
            groups.push(h(
              'div',
              { className: 'dm-group', key: 'current' },
              h('div', { className: 'dm-group-head' },
                h('div', { className: 'dm-group-title' },
                  h('span', null, '当前工作区 · ' + (currentTitle || currentRef)),
                ),
                h('span', { className: 'dm-group-count' }, currentItems.length + ' 条'),
              ),
              h('div', { className: 'dm-group-body' },
                currentItems.length
                  ? renderItemList(currentItems, 'cur')
                  : h('div', { className: 'dm-group-empty' }, '这个工作区还没有专属记忆'),
              ),
            ))
          }

          // ② 其它工作区（可折叠，内部再按 scope_ref 分小组）
          if (otherGroups.length) {
            /* 用工厂函数隔离每个子分组的闭包：var 在循环里会被共用，
             * 直接内联会把所有按钮都绑到最后一组（点一个全展开）。 */
            var buildSubgroup = function (grp, isFolded, groupIndex) {
              return h(
                'div',
                { className: 'dm-subgroup', key: 'sub-' + grp.ref },
                h('div', { className: 'dm-subgroup-head' },
                  h('span', { className: 'dm-subgroup-title' },
                    h('button', {
                      className: 'dm-group-toggle',
                      type: 'button',
                      'aria-expanded': !isFolded,
                      onClick: function () {
                        var next = {}
                        var k
                        for (k in folded) {
                          if (Object.prototype.hasOwnProperty.call(folded, k)) next[k] = folded[k]
                        }
                        next[grp.ref] = !isFolded
                        setFolded(next)
                      },
                    },
                      h('span', { className: 'dm-group-caret' + (isFolded ? '' : ' open') }, '›'),
                      grp.title,
                    ),
                  ),
                  h('span', { className: 'dm-group-count' }, grp.rows.length + ' 条'),
                ),
                isFolded ? null : renderItemList(grp.rows, 'o-' + groupIndex),
              )
            }
            var otherBodies = []
            var g
            for (g = 0; g < otherGroups.length; g += 1) {
              otherBodies.push(buildSubgroup(otherGroups[g], folded[otherGroups[g].ref] === true, g))
            }
            groups.push(h(
              'div',
              { className: 'dm-group', key: 'other' },
              h('div', { className: 'dm-group-head' },
                h('button', {
                  className: 'dm-group-toggle',
                  type: 'button',
                  'aria-expanded': otherOpen,
                  onClick: function () { setOtherOpen(!otherOpen) },
                },
                  h('span', { className: 'dm-group-caret' + (otherOpen ? ' open' : '') }, '›'),
                  '其它工作区',
                ),
                h('span', { className: 'dm-group-count' }, otherGroups.length + ' 组'),
              ),
              otherOpen ? h('div', { className: 'dm-group-body' }, otherBodies) : null,
            ))
          }

          // ③ 用户画像（全局）
          if (globalItems.length) {
            groups.push(h(
              'div',
              { className: 'dm-group', key: 'global' },
              h('div', { className: 'dm-group-head' },
                h('div', { className: 'dm-group-title' }, '用户画像（全局）'),
                h('span', { className: 'dm-group-count' }, globalItems.length + ' 条'),
              ),
              h('div', { className: 'dm-group-body' }, renderItemList(globalItems, 'glb')),
            ))
          }

          list = h('div', { className: 'dm-groups', key: 'groups' }, groups)
        }

        /* 识别不到工作区时，无论列表空不空都在最上方挂一条提示（契约 §5） */
        if (!currentRef) {
          list = [
            h('div', { className: 'dm-hintbar', key: 'nocwd' }, '未识别到当前工作区，仅显示用户记忆（全局）。'),
            list,
          ]
        }

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

          /* 多源链上线后才有 source / diagnostics / quota；字段缺失时整块不渲染 */
          var sourceLabel = (info && typeof info.source === 'string' && info.source) ? info.source : ''
          var diagRows = (info && info.diagnostics && info.diagnostics.length) ? info.diagnostics : []
          var quotaText = ''
          if (info && info.quota && typeof info.quota === 'object') {
            var quotaRemaining = info.quota.remaining
            if (quotaRemaining !== undefined && quotaRemaining !== null) {
              quotaText = '匿名配额剩余 ' + quotaRemaining
              if (info.quota.reset) quotaText += ' · 重置 ' + String(info.quota.reset)
            }
          }

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
            sourceLabel ? h('div', { className: 'dm-update-source' }, '成功源：' + sourceLabel + (quotaText ? ' · ' + quotaText : '')) : null,
            (info && info.error && diagRows.length)
              ? h('details', { className: 'dm-update-diag' },
                  h('summary', null, '查看诊断（' + diagRows.length + ' 个源）'),
                  h('ul', null,
                    diagRows.map(function (d, i) {
                      var row = d || {}
                      var src = row.source || row.url || ('源 ' + (i + 1))
                      var bits = []
                      if (row.status !== undefined && row.status !== null) bits.push('HTTP ' + row.status)
                      if (row.ms !== undefined && row.ms !== null) bits.push(row.ms + 'ms')
                      return h('li', { key: i },
                        h('span', { className: 'dm-update-diag-src' }, src),
                        bits.length ? ' · ' + bits.join(' · ') : null,
                        row.error ? h('span', { className: 'dm-update-diag-err' }, ' · ' + row.error) : null,
                      )
                    }),
                  ),
                )
              : null,
            h(
              'div',
              { className: 'dm-update-actions' },
              h('button', {
                className: 'dm-btn',
                disabled: !!busy,
                onClick: function () { props.onCheck() },
              }, busy === 'checking' ? '检查中…' : '检查更新'),
              h('button', {
                className: 'dm-btn tiny',
                disabled: !!busy,
                title: '忽略 6 小时缓存与失败退避，立即重新探测所有源',
                onClick: function () { props.onForceCheck() },
              }, '强制检查'),
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

        /* ── 页签条（学 our-free-model 的 ofm_tabs 交互）───────────────────
         * 记忆页放最前（用户最常看），带条数角标；后三类是低频操作。 */
        var tabs = [
          ['memory', '记忆', items.length],
          ['engine', '巩固引擎', null],
          ['settings', '设置', null],
          ['upgrade', '插件升级', null],
        ]
        var tabBar = h('div', { className: 'dm-tabs', role: 'tablist' },
          tabs.map(function (t) {
            return h('button', {
              key: t[0],
              type: 'button',
              role: 'tab',
              'aria-selected': tab === t[0] ? 'true' : 'false',
              className: 'dm-tab',
              onClick: function () { setTab(t[0]) },
            }, t[1], t[2] === null ? null : h('span', { className: 'dm-tabnum' }, String(t[2])))
          }))

        /* ── 各页内容：照 our-free-model 的「每页一个块」结构 ───────────── */
        var page = null
        if (tab === 'memory') {
          page = h(
            'div',
            { className: 'dm-card' },
            h(
              'div',
              { className: 'dm-card-head' },
              h('div', { className: 'dm-card-title' },
                '记忆',
                currentRef
                  ? h('span', { className: 'dm-tag' }, currentTitle || currentRef)
                  : null,
              ),
              h('span', { className: 'dm-hint' },
                items.length + ' 条'
                + (currentRef ? ' · 当前 ' + currentItems.length : ' · 未识别工作区')
                + (msg ? ' · ' + msg : '')),
            ),
            list,
          )
        } else if (tab === 'engine') {
          page = h(
            'div',
            { className: 'dm-card' },
            h(
              'div',
              { className: 'dm-card-head' },
              h('div', { className: 'dm-card-title' },
                h('span', { className: 'dm-dot ' + (llm && llm.configured ? 'on' : 'off') }),
                '巩固引擎（LLM）',
              ),
              /* 引擎徽标：真实判定由后端 engine_status 给出，不靠前端猜 */
              engineRow,
            ),
            llmBlock,
            engineBlock,
          )
        } else if (tab === 'settings') {
          /* 通用设置页：把「可选增强」做成看得见、点得动的开关 */
          var autoOn = settings ? settings.autoConsolidate === true : false
          var dailyOn = settings ? settings.dailyConsolidate === true : false
          var settingsKnown = settings !== null
          page = h(
            'div',
            { className: 'dm-settings' },
            h('div', { className: 'dm-settings-title' }, '通用设置'),
            h('div', { className: 'dm-setting-row' },
              h('div', null,
                h('div', { className: 'dm-setting-name' }, '自动整理记忆'),
                h('div', { className: 'dm-setting-hint' },
                  '收件箱攒够后自动抽取并巩固（无需手动跑命令或配定时任务）。',
                  '关闭后仍会采集与注入，但不再自动整理，除非你手动跑 flush-inbox。')),
              h(Switch, {
                checked: autoOn,
                disabled: !settingsKnown || settingsBusy,
                onChange: function () { void saveSetting('autoConsolidate', !autoOn) },
              }),
            ),
            h('div', { className: 'dm-setting-row' },
              h('div', null,
                h('div', { className: 'dm-setting-name' }, '每日定时整理（LLM 巩固）'),
                h('div', { className: 'dm-setting-hint' },
                  '每天在固定时刻整理一次，并顺带做生命周期维护（依赖消失 / TTL）。',
                  '会真实调用上面的 LLM（没配则走规则）。与上面的自动整理可以并存。',
                  '对应旧版「LLM 每日巩固」定时任务——现在不用再配外部任务了。')),
              h(Switch, {
                checked: dailyOn,
                disabled: !settingsKnown || settingsBusy,
                onChange: function () { void saveSetting('dailyConsolidate', !dailyOn) },
              }),
            ),
            h('div', { className: 'dm-setting-row' },
              h('div', null,
                h('div', { className: 'dm-setting-name' }, '每日整理时刻'),
                h('div', { className: 'dm-setting-hint' }, '24 小时制 HH:MM，本地时间；默认凌晨 03:00。')),
              h('input', {
                className: 'dm-key-input',
                type: 'text',
                style: { width: 90, textAlign: 'center' },
                value: dailyTimeInput,
                placeholder: '03:00',
                disabled: !settingsKnown || settingsBusy,
                onChange: function (event) { dailyTimeDirty.current = true; setDailyTimeInput(event.target.value) },
                onKeyDown: function (event) {
                  if (event.key === 'Enter') void commitDailyTime()
                },
              }),
              h('button', {
                className: 'dm-btn tiny',
                disabled: !settingsKnown || settingsBusy,
                onClick: function () { void commitDailyTime() },
              }, '保存时刻'),
            ),
            h('div', { className: 'dm-hint' },
              settingsKnown
                ? '开关保存在本机配置（' + '$DSH_HOME/dsh-tide-memory.json' + '），立即生效。'
                : '设置接口不可用（后端可能还没升级到 2.2.0）——此时请手动编辑配置文件。'),
          )
        } else {
          page = h(UpdateCard, {
            info: updateInfo,
            busy: updateBusy,
            onCheck: function () { void loadUpdate(false) },
            onForceCheck: function () { void loadUpdate(true) },
            onApply: function () { void runUpdate() },
            onStar: function () { void openRepo() },
          })
        }

        var body = h(
          'div',
          { className: 'dm-root' },
          h(
            'div',
            { className: 'dm-head' },
            h('h2', null, h(PluginIcon, null), '长期记忆'),
            tab === 'memory'
              ? h('button', { className: 'dm-btn primary', onClick: openNew }, '＋ 新增记忆')
              : null,
          ),
          tabBar,
          page,
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

        /* ── 可选服务探测（sessions / workspaces / uiWorkspace）─────────────
         *
         * 这里刻意【不】把它们写进 settings.section 的 inject 依赖列表：
         * cordis 的 inject 是「全部就绪才回调」，一旦 uiWorkspace 没注册
         * （无头环境、裁剪版宿主），整块设置页就永远不注册——那才是真白屏。
         * 所以：
         *   ① 主路径只依赖 slots，保证 section 一定注册得上；
         *   ② 再单独 inject 一次这三个服务，拿到了就缓存，拿不到就算了；
         *   ③ 面板内部还有 owner.get 兜底 + 全程 try/catch。
         * 三层下来最差情况是「认不出工作区」，列表照常显示 global 记忆。
         */
        var serviceCache = {}
        function remember(name) {
          return function (owner) {
            try {
              var svc = owner && typeof owner.get === 'function' ? owner.get(name) : (owner ? owner[name] : null)
              if (svc !== undefined && svc !== null) serviceCache[name] = svc
            } catch (error) {
              /* 拿不到就算了，面板还有 owner.get 兜底 */
            }
          }
        }
        var optionalNames = ['sessions', 'workspaces', 'uiWorkspace']
        var optionalIdx
        for (optionalIdx = 0; optionalIdx < optionalNames.length; optionalIdx += 1) {
          try {
            ctx.inject([optionalNames[optionalIdx]], remember(optionalNames[optionalIdx]))
          } catch (error) {
            console.warn('[dsh-tide-memory] 可选服务 ' + optionalNames[optionalIdx] + ' 探测失败，已降级', error)
          }
        }

        var Panel = makePanel(React, ReactDOM, {
          cache: serviceCache,
          resolve: function (name) {
            if (!name) return null
            if (Object.prototype.hasOwnProperty.call(serviceCache, name)) return serviceCache[name]
            return null
          },
        })

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
