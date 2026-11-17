type FinancialFieldEvidence = {
  source_url?:string;document_sha256?:string;page?:number;quote?:string;unit?:string;scale?:number;value?:number;
  verification_status?:string;
};
export type BuffettFundamentals = {
  status: "valid" | "insufficient" | "invalid";
  issues: string[];
  eligible: boolean;
  financial_valid?:boolean;assessment_valid?:boolean;annual_structure_valid?:boolean;
  reports: {
    period_end: string; available_on: string;
    operating_cashflow: number | null; maintenance_capex: number | null; net_profit: number | null;
    roe_pct: number | null; debt_to_equity: number | null; shares_outstanding: number | null;
    point_in_time_verified?:boolean;field_evidence?:Record<string,FinancialFieldEvidence>;
  }[];
  assessment: { available_on: string; within_circle: boolean | null; moat_supported: boolean | null; note: string } | null;
  valuation: {
    owner_cashflow_proxy_per_share: number | null; base_cashflow_proxy_per_share?: number | null;
    conservative_value: number | null; base_value: number | null; buy_ceiling: number | null;
    margin_of_safety_pct: number | null; discount_rate_pct: number;
    conservative_growth_pct: number; base_growth_pct: number; terminal_growth_pct: number; forecast_years: number;
  } | null;
  checks: { code: string; label: string; passed: boolean; detail: string }[];
  raw_reports?: {
    period_end:string;available_on:string;operating_cashflow?:number|null;reported_capex?:number|null;
    net_profit?:number|null;roe_pct?:number|null;debt_to_equity?:number|null;shares_outstanding?:number|null;
    conservative_total_capex_proxy?:number|null;source_url?:string;point_in_time_verified?:boolean;
  }[];
  data_status?:string;
  data_coverage?:{
    expected_years:string[];reference_year_count:number;
    items:{key:string;label:string;status:"reference_only"|"missing"|"manual_required"|"unverified";available_count:number;required_count:number;detail:string}[];
  };
  data_provenance?:{source?:string;source_url?:string;retrieved_at?:string;point_in_time_verified?:boolean};
  limitations?:string[];
};

const number = (value: number | undefined | null, digits = 2) => value != null && Number.isFinite(value)
  ? value.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "—";

const coverageLabels:Record<string,string>={reference_only:"已下载参考",missing:"仍缺失",manual_required:"待人工确认",unverified:"待原文核验"};
const fieldLabels:Record<string,string>={operating_cashflow:"经营现金流",maintenance_capex:"维持性资本开支",net_profit:"归母净利润",roe_pct:"加权平均ROE",debt_to_equity:"总负债 / 权益比",shares_outstanding:"同期总股数",reported_capex:"报表总资本开支",total_liabilities:"总负债",total_equity:"总权益"};
const originalHosts=new Set(["static.cninfo.com.cn","www.cninfo.com.cn","www.sse.com.cn","sse.com.cn","disc.static.szse.cn","www.szse.cn","szse.cn"]);

function sourceLink(value?:string):string|undefined {
  if(typeof value!=="string")return undefined;
  try{
    const url=new URL(value);
    if(url.protocol==="https:" && !url.username && !url.password && !url.port && (url.hostname==="eastmoney.com" || url.hostname.endsWith(".eastmoney.com") || originalHosts.has(url.hostname)))return url.href;
  }catch{}
  return undefined;
}

export function BuffettEvidence({ fundamentals }: { fundamentals?: BuffettFundamentals }) {
  if (!fundamentals) return <p className="section-note">未收到价值评估证据，不能据日线推断基本面。</p>;
  const { valuation, assessment, reports, checks } = fundamentals;
  const rawReports=fundamentals.raw_reports ?? [];
  const referenceUrl=sourceLink(fundamentals.data_provenance?.source_url);
  const coverage=fundamentals.data_coverage;
  const pendingOriginalVersion=reports.some(report=>report.point_in_time_verified===false);
  const evidenceRows=reports.slice(-5).flatMap(report=>Object.entries(report.field_evidence ?? {}).filter(([field,proof])=>Object.hasOwn(fieldLabels,field)&&proof&&typeof proof==="object").slice(0,9).map(([field,proof])=>({period:report.period_end,field,proof})));
  const hasMatchedOriginal=evidenceRows.some(({proof})=>proof.verification_status==="matched_original"&&Number.isSafeInteger(proof.page)&&proof.page!>0&&proof.page!<=800&&typeof proof.document_sha256==="string"&&/^[a-f0-9]{64}$/.test(proof.document_sha256)&&sourceLink(proof.source_url));
  return <div className="buffett-evidence">
    <div className="section-heading"><h3>现金流情景估值与安全边际</h3></div>
    <p className="section-note">规则受巴菲特思想启发，并非本人授权或训练模型；护城河与能力圈依据人工评估，不由价格猜测。</p>
    {(fundamentals.financial_valid!==undefined||fundamentals.assessment_valid!==undefined)&&<p className="section-note" aria-label="价值证据分项状态">财务完整核验：{fundamentals.financial_valid===true?"通过":"未完成或未通过"} · 人工业务确认：{fundamentals.assessment_valid===true?"通过":"待确认或未通过"} · 最终买入门槛：{fundamentals.eligible?"条件满足，仍需撮合与组合检查":"未通过，保持观望"}</p>}
    {pendingOriginalVersion&&<div className="callout" role="note"><strong>{hasMatchedOriginal?"原文已核对事实，仍待历史更正版本与人工评估":"原始报告已接入，字段与历史版本仍待核验"}</strong><p>仅有有效来源依据的字段可分别判断利润、资本回报等条件；不代表所有年度财务、维持性开支、同期股数或买入条件已齐备，未核验版本不会自动放行。</p></div>}
    {coverage && Array.isArray(coverage.items) && <section className="real-financial-reference" aria-label="巴菲特数据补齐清单">
      <div className="section-heading"><h4>巴菲特数据清单</h4><span className="pill">参考完整年度 {number(coverage.reference_year_count,0)} / 3</span></div>
      <p className="section-note">下载完成不等于买入条件通过。公开报表、原始版本核验、维持性资本开支与人工业务确认分别检查，不用总开支、当前股本或乐观假设补空值。</p>
      <div className="table-scroll" tabIndex={0} role="region" aria-label="价值策略必需数据与缺口"><table className="buffett-report-table"><thead><tr><th>必需资料</th><th>覆盖</th><th>状态</th><th>说明 / 下一步</th></tr></thead><tbody>{coverage.items.slice(0,16).map((item,index)=><tr key={`${item.key}-${index}`}><th>{item.label}</th><td>{item.status==="manual_required"?"人工判断":`${number(item.available_count,0)} / ${number(item.required_count,0)}`}</td><td>{coverageLabels[item.status] || "待核对"}</td><td>{item.detail}</td></tr>)}</tbody></table></div>
    </section>}
    {fundamentals.data_status && <section className="real-financial-reference" aria-label="真实财务资料核对">
      <div className="section-heading"><h4>真实财务资料核对</h4><span className="pill">{rawReports.length ? `${rawReports.length}期参考` : "尚未取得资料"}</span></div>
      <p className="section-note">{rawReports.length ? "以下为当前检索的历史年度报表资料。原始历史版本、重述情况尚未核验，未作为当时已知的决策证据，也不自动通过价值门槛。" : "该股票未取得可展示的年度财务资料；保留真实价格，缺失值不填零、不补合成财报。"}</p>
      {fundamentals.data_provenance?.source && <p className="section-note">来源：{fundamentals.data_provenance.source}{referenceUrl && <> · <a href={referenceUrl} target="_blank" rel="noopener noreferrer">查看来源</a></>}{fundamentals.data_provenance.retrieved_at && <> · 检索于 {fundamentals.data_provenance.retrieved_at}</>}</p>}
      {rawReports.length>0 && <div className="table-scroll" tabIndex={0} role="region" aria-label="真实年度财务参考表"><table className="buffett-report-table"><thead><tr><th>年度</th><th>源标注公告日</th><th>经营现金流</th><th>购建长期资产支出</th><th>归母净利润</th><th>ROE</th><th>总负债 / 权益</th><th>经营现金流－总开支</th></tr></thead><tbody>{rawReports.slice(-5).map(report=><tr key={report.period_end}><th>{report.period_end}</th><td>{report.available_on}</td><td>{number(report.operating_cashflow)}</td><td>{number(report.reported_capex)}</td><td>{number(report.net_profit)}</td><td>{number(report.roe_pct)}%</td><td>{number(report.debt_to_equity)}×</td><td>{number(report.conservative_total_capex_proxy)}</td></tr>)}</tbody></table></div>}
      {rawReports.length>0 && <p className="section-note">金额单位：百万元。购建长期资产现金支出不等于维持性资本开支；总负债/权益不等于有息债务/权益。末列仅为公开字段相减的参考，不是已核验的所有者收益、估值或买入依据。</p>}
      {(fundamentals.limitations?.length ?? 0)>0 && <details className="disclosure"><summary>数据缺口与使用边界</summary><ul>{fundamentals.limitations?.map((note,index)=><li key={index}>{note}</li>)}</ul></details>}
    </section>}
    {fundamentals.status !== "valid" && <div className={`callout${fundamentals.status === "invalid" ? " danger" : ""}`}>
      <strong>{fundamentals.status === "invalid" ? "财报数据异常 · 阻止回放" : "证据不足 · 保持观望"}</strong>
      <ul>{fundamentals.issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul>
    </div>}
    {valuation?.conservative_value != null && <>
      <dl className="metric-grid buffett-value-grid">
        <div><dt>保守估值 / 股</dt><dd>{number(valuation.conservative_value)} 元</dd></div>
        <div><dt>基准估值 / 股</dt><dd>{number(valuation.base_value)} 元</dd></div>
        <div><dt>含安全边际买入上限</dt><dd>≤ {number(valuation.buy_ceiling)} 元</dd></div>
        <div><dt>当前安全边际</dt><dd>{number(valuation.margin_of_safety_pct)}%</dd></div>
      </dl>
      <p className="section-note">估值取决于现金流和增长假设，不是保证可卖出的目标价。安全边际不足，或生意与财务门槛未通过，均不入场。</p>
    </>}
    {assessment && <p className="buffett-assessment"><strong>生意评估</strong> · 能力圈 {assessment.within_circle ? "通过" : "未通过"} · 护城河证据 {assessment.moat_supported ? "通过" : "不足"}<br />
      {assessment.note}<small>评估可用日期 {assessment.available_on}</small>
    </p>}
    {checks.length > 0 && <details className="disclosure">
      <summary>价值门槛：{checks.filter((check) => check.passed).length} / {checks.length} 项满足</summary>
      <ul className="checks">{checks.map((check, index) => <li key={`${check.code}-${index}`}><span className="pill">{check.passed ? "满足" : "不满足"}</span><div><strong>{check.label}</strong><p>{check.detail}</p></div></li>)}</ul>
    </details>}
    {evidenceRows.length>0&&<details className="disclosure"><summary>查看原文逐字段核对依据（{evidenceRows.length}项）</summary><div className="table-scroll" tabIndex={0} role="region" aria-label="财务字段原文页码与来源"><table className="buffett-report-table"><thead><tr><th>年度 / 字段</th><th>PDF页码</th><th>报表单位</th><th>原文摘录与来源</th></tr></thead><tbody>{evidenceRows.map(({period,field,proof},index)=>{
      const url=sourceLink(proof.source_url),page=Number.isSafeInteger(proof.page)&&proof.page!>0&&proof.page!<=800?proof.page:null;
      const knownProof=proof.verification_status==="matched_original"&&typeof proof.document_sha256==="string"&&/^[a-f0-9]{64}$/.test(proof.document_sha256)&&page!==null&&url;
      return <tr key={`${period}-${field}-${index}`}><th>{period}<br/>{fieldLabels[field]}</th><td>{page??"—"}</td><td>{typeof proof.unit==="string"?proof.unit.slice(0,60):"—"}</td><td><p>{typeof proof.quote==="string"?proof.quote.slice(0,500):"未保存摘录"}</p>{url&&<a href={url} target="_blank" rel="noopener noreferrer">查看披露原文</a>}<p className="section-note">{knownProof?"此字段已与原文数值核对，历史版本另行核验。":"来源记录仍待核对，不作为放行证明。"}</p></td></tr>;
    })}</tbody></table></div></details>}
    <details className="disclosure">
      <summary>{pendingOriginalVersion?"原文财报事实与估值假设":"当时可见财报与估值假设"}（{reports.length} 期）</summary>
      <p className="section-note">{pendingOriginalVersion?"原始披露事实与严格时点核验分开，缺字段保持空值，不冒充已完成历史版本核验。":"只纳入决策日前已公开的年度报告。"}金额单位：百万元；股本：百万股；现金流代理 = 经营现金流 − 维持性资本开支，不等同于精确的“所有者收益”。</p>
      {reports.length > 0 && <div className="table-scroll" tabIndex={0} role="region" aria-label="巴菲特席位时点财报表">
        <table className="buffett-report-table"><thead><tr>
          <th scope="col">报告期</th><th scope="col">可用日期</th><th scope="col">经营现金流</th><th scope="col">维持性开支</th><th scope="col">净利润</th><th scope="col">ROE</th><th scope="col">债务 / 权益</th><th scope="col">股本</th>
        </tr></thead><tbody>{reports.map((report) => <tr key={report.period_end}>
          <th scope="row">{report.period_end}</th><td>{report.available_on}</td><td>{number(report.operating_cashflow)}</td><td>{number(report.maintenance_capex)}</td><td>{number(report.net_profit)}</td><td>{number(report.roe_pct)}%</td><td>{number(report.debt_to_equity)}×</td><td>{number(report.shares_outstanding)}</td>
        </tr>)}</tbody></table>
      </div>}
      {valuation && <p className="section-note">{valuation.forecast_years} 年现金流折现 + 终值；折现率 {number(valuation.discount_rate_pct)}%，保守增长 {number(valuation.conservative_growth_pct)}%，基准增长 {number(valuation.base_growth_pct)}%，永续增长 {number(valuation.terminal_growth_pct)}%。保守现金流代理 {number(valuation.owner_cashflow_proxy_per_share)} 元/股，基准 {number(valuation.base_cashflow_proxy_per_share)} 元/股。所有数值门槛是本项目演示参数，并非巴菲特规定的固定标准。</p>}
    </details>
  </div>;
}
