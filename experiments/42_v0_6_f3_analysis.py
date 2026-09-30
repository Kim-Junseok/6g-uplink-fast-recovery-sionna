#!/usr/bin/env python3
"""Analyze F(K=3) against the accepted H(K=3) paired control."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import itertools
import json
import math
import statistics
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


SEEDS = tuple(range(9101, 9109))
SCENARIOS = {
    "B": "V0_5_RHO090_FAST_SB",
    "H2": "V0_6_RHO090_H_HARQ_PRESERVING_SHARED_CAP2",
    "F2": "V0_6_RHO090_F_FRESH_HARQ_SHARED_CAP2",
    "H3": "V0_6_RHO090_HK3_HARQ_PRESERVING_SHARED_CAP2",
    "F3": "V0_6_RHO090_FK3_FRESH_HARQ_SHARED_CAP2",
}
CLASSIFICATIONS = (
    "K3-HARQ-PRESERVATION-SUPPORTED",
    "K3-HARQ-PRESERVATION-CONDITIONAL",
    "K3-HARQ-PRESERVATION-NOT-RESOLVED",
    "K3-HARQ-PRESERVATION-NOT-SUPPORTED",
)
T95_7 = 2.364624251


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--b-raw-root", type=Path,
                        default=Path("results/raw_v0_5"))
    parser.add_argument("--p3-raw-root", type=Path,
                        default=Path("results/raw_v0_6_p3"))
    parser.add_argument("--h3-raw-root", type=Path,
                        default=Path("results/raw_v0_6_k3"))
    parser.add_argument("--f3-raw-root", type=Path,
                        default=Path("results/raw_v0_6_f3"))
    parser.add_argument("--output", type=Path,
                        default=Path("results/v0_6_f3"))
    parser.add_argument("--config", type=Path,
                        default=Path("configs/p3_fk3_v0_6.yaml"))
    parser.add_argument("--implementation-sha", required=True)
    parser.add_argument("--results-sha", required=True)
    parser.add_argument("--classification", choices=CLASSIFICATIONS,
                        required=True)
    parser.add_argument("--rho070-decision",
                        choices=("RHO070-GO", "RHO070-HOLD"), required=True)
    return parser.parse_args()


def rows(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def truth(value):
    return value is True or str(value).lower() == "true"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q / 100
    low, high = math.floor(position), math.ceil(position)
    return (values[low] if low == high else values[low] +
            (position-low)*(values[high]-values[low]))


def write_csv(path, data):
    if not data:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(data[0]),
                                lineterminator="\n")
        writer.writeheader(); writer.writerows(data)


def directory(args, scheme, seed):
    root = {"B": args.b_raw_root, "H2": args.p3_raw_root,
            "F2": args.p3_raw_root, "H3": args.h3_raw_root,
            "F3": args.f3_raw_root}[scheme]
    return root / SCENARIOS[scheme] / f"seed_{seed}"


def load(args, scheme, seed):
    path = directory(args, scheme, seed)
    manifest = json.loads((path/"run_manifest.json").read_text())
    summary = json.loads((path/"summary.json").read_text())
    packets, attempts = rows(path/"packets.csv.gz"), rows(path/"attempts.csv.gz")
    slots = rows(path/"slot_prb.csv.gz")
    hashes = {name: sha256(path/name) for name in manifest["output_sha256"]}
    if (hashes != manifest["output_sha256"] or len(packets) != 2500 or
            summary["packets"]["unfinished_after_drain"]):
        raise RuntimeError(f"invalid input run: {scheme}/{seed}")
    return manifest, summary, packets, attempts, slots


def kpis(scheme, seed, summary, packets, attempts, slots):
    correct = [row for row in packets if truth(row["delivered_correctly"])]
    latency = [float(row["completion_latency_slots"]) for row in correct]
    scheduled = [row for row in attempts if row["access"] == "scheduled_rescue"]
    cb = [row for row in attempts if row["access"] == "grant_free"]
    rescued = {row["payload_id"] for row in scheduled}
    first, scheduled_counts = {}, Counter(row["payload_id"] for row in scheduled)
    for row in scheduled:
        key = row["payload_id"]
        if (key not in first or int(row["actual_tx_slot"]) <
                int(first[key]["actual_tx_slot"])):
            first[key] = row
    queue = [int(row["scheduled_queue_length_before_service"]) for row in slots]
    waits = [int(row["scheduled_queue_wait"]) for row in scheduled
             if row["scheduled_queue_wait"] not in (None, "")]
    residual = [int(row["cb_prbs_available"]) for row in slots]
    rlc = [int(row["rlc_retx_count"]) for row in packets]
    first_successes = sum(truth(row["ack"]) for row in first.values())
    return {"scheme":scheme, "seed":seed,
        "receiver_acceptance_probability":sum(truth(r["receiver_accepted"]) for r in packets)/2500,
        "correct_delivery_probability":len(correct)/2500,
        "undetected_error_probability":sum(truth(r["undetected_error"]) for r in packets)/2500,
        "terminal_failure_probability":sum(r["oracle_terminal_status"]=="protocol_drop" for r in packets)/2500,
        "mean_latency":statistics.fmean(latency),
        "median_latency":statistics.median(latency),
        "p95_latency":percentile(latency,95), "p99_latency":percentile(latency,99),
        "max_latency":max(latency), "phy_attempts_per_packet":len(attempts)/2500,
        "harq_episodes_per_packet":statistics.fmean(int(r["num_harq_episodes"]) for r in packets),
        "rlc_retransmissions_per_packet":statistics.fmean(rlc),
        "rescued_packet_count":len(rescued),
        "first_scheduled_success_count":first_successes,
        "two_plus_scheduled_attempt_packet_count":sum(v>=2 for v in scheduled_counts.values()),
        "first_scheduled_success_probability":first_successes/len(first) if first else 0.0,
        "scheduled_attempts_per_rescued_packet":len(scheduled)/len(rescued) if rescued else 0.0,
        "scheduled_attempts_per_packet":len(scheduled)/2500,
        "scheduled_prb_slots":len(scheduled),
        "cb_prb_slots":len({(r["actual_tx_slot"],r["prb"]) for r in cb}),
        "total_occupied_prb_slots":sum(int(r["total_prbs_occupied"]) for r in slots),
        "mean_queue":statistics.fmean(queue), "p95_queue":percentile(queue,95),
        "max_queue":max(queue), "mean_wait":statistics.fmean(waits) if waits else 0.0,
        "p95_wait":percentile(waits,95) if waits else 0.0,
        "fraction_at_cap2":sum(int(r["scheduled_service_count"])==2 for r in slots)/len(slots),
        "mean_residual_cb_prbs":statistics.fmean(residual),
        "runtime_seconds":summary["runtime"]["wall_seconds"],
        "peak_rss_kib":summary["runtime"]["peak_rss_kib"]}


def paired(table, metric):
    lookup={(row["scheme"],int(row["seed"])):float(row[metric]) for row in table}
    values=[lookup[("H3",seed)]-lookup[("F3",seed)] for seed in SEEDS]
    mean, sd = statistics.fmean(values), statistics.stdev(values)
    se = sd/math.sqrt(8)
    pvalue=sum(abs(sum(sign*v for sign,v in zip(signs,values))/8)>=abs(mean)-1e-15
               for signs in itertools.product((-1,1),repeat=8))/256
    loo=[statistics.fmean(values[:i]+values[i+1:]) for i in range(8)]
    return values,{"comparison":"H3-F3","metric":metric,
        "mean_difference":mean,"sample_sd":sd,"standard_error":se,
        "ci95_low":mean-T95_7*se,"ci95_high":mean+T95_7*se,
        "exact_sign_flip_p":pvalue,"leave_one_out_min":min(loo),
        "leave_one_out_max":max(loo),
        "leave_one_out_sign_stable":min(loo)>0 or max(loo)<0}


def curves(all_runs):
    maximum=int(max(float(row["completion_latency_slots"])
        for scheme in ("B","H3","F3") for seed in SEEDS
        for row in all_runs[(scheme,seed)][2] if truth(row["delivered_correctly"])))
    grid=np.arange(maximum+1); output=[]
    for scheme in ("B","H3","F3"):
        values=[]
        for seed in SEEDS:
            latency=sorted(float(r["completion_latency_slots"])
                for r in all_runs[(scheme,seed)][2] if truth(r["delivered_correctly"]))
            values.append(np.searchsorted(latency,grid,side="right")/2500)
        array=np.asarray(values); mean=array.mean(axis=0)
        half=T95_7*array.std(axis=0,ddof=1)/math.sqrt(8)
        output += [{"scheme":scheme,"latency_slots":int(x),"mean":y,
                    "ci95_low":max(0,y-d),"ci95_high":min(1,y+d)}
                   for x,y,d in zip(grid,mean,half)]
    return output


def save(fig,base):
    fig.tight_layout()
    for suffix in ("png","svg","pdf"):
        fig.savefig(base.with_suffix(f".{suffix}"),dpi=200)
    plt.close(fig)


def curve_plot(data,base,tail=False):
    fig,ax=plt.subplots(figsize=(6.4,4.0))
    colors={"B":"#777777","H3":"#0072B2","F3":"#D55E00"}
    for scheme in colors:
        group=[r for r in data if r["scheme"]==scheme]
        x=np.array([r["latency_slots"] for r in group])
        y=np.array([r["mean"] for r in group])
        lo=np.array([r["ci95_low"] for r in group]); hi=np.array([r["ci95_high"] for r in group])
        ax.plot(x,y,label=scheme,color=colors[scheme],
                linestyle="--" if scheme=="B" else "-")
        ax.fill_between(x,lo,hi,alpha=.13,color=colors[scheme])
    ax.set_xlabel("Latency (slots)"); ax.set_ylabel("Correct-delivery probability")
    if tail: ax.set_ylim(.85,1.005)
    ax.grid(alpha=.25); ax.legend(); save(fig,base)


def bars(table,metrics,titles,base,schemes):
    fig,axes=plt.subplots(1,len(metrics),figsize=(3.0*len(metrics),3.7))
    colors={"B":"#777777","H2":"#56B4E9","F2":"#E69F00",
            "H3":"#0072B2","F3":"#D55E00"}
    for ax,metric,title in zip(np.atleast_1d(axes),metrics,titles):
        groups=[[float(r[metric]) for r in table if r["scheme"]==s] for s in schemes]
        ax.bar(schemes,[statistics.fmean(x) for x in groups],
               yerr=[T95_7*statistics.stdev(x)/math.sqrt(8) for x in groups],
               color=[colors[s] for s in schemes],capsize=3)
        ax.set_title(title); ax.grid(axis="y",alpha=.25)
    save(fig,base)


def main():
    args=parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    all_runs={(scheme,seed):load(args,scheme,seed)
              for scheme in SCENARIOS for seed in SEEDS}
    table=[kpis(scheme,seed,*all_runs[(scheme,seed)][1:])
           for scheme in SCENARIOS for seed in SEEDS]
    write_csv(args.output/"run_level_kpis.csv",table)
    metrics=("correct_delivery_probability","terminal_failure_probability",
        "undetected_error_probability","mean_latency","median_latency","p95_latency",
        "p99_latency","max_latency","first_scheduled_success_probability",
        "scheduled_attempts_per_rescued_packet","scheduled_attempts_per_packet",
        "harq_episodes_per_packet","phy_attempts_per_packet","mean_queue","p95_queue",
        "max_queue","mean_wait","p95_wait","fraction_at_cap2","scheduled_prb_slots",
        "mean_residual_cb_prbs","total_occupied_prb_slots",
        "rlc_retransmissions_per_packet")
    differences,summaries=[],[]
    for metric in metrics:
        values,summary=paired(table,metric); summaries.append(summary)
        differences += [{"comparison":"H3-F3","metric":metric,"seed":seed,
                         "difference":value} for seed,value in zip(SEEDS,values)]
    write_csv(args.output/"paired_seed_differences.csv",differences)
    write_csv(args.output/"paired_statistics.csv",summaries)
    write_csv(args.output/"per_seed_tail.csv",[{k:r[k] for k in
        ("scheme","seed","p95_latency","p99_latency","max_latency")}
        for r in table if r["scheme"] in {"H3","F3"}])
    write_csv(args.output/"first_scheduled_success.csv",[{k:r[k] for k in
        ("scheme","seed","rescued_packet_count","first_scheduled_success_count",
         "two_plus_scheduled_attempt_packet_count",
         "first_scheduled_success_probability",
         "scheduled_attempts_per_rescued_packet")}
        for r in table if r["scheme"] in {"H3","F3"}])
    completion=curves(all_runs)
    write_csv(args.output/"completion_curve_source.csv",completion)
    curve_plot(completion,args.output/"v0_6_f3_a_completion")
    curve_plot(completion,args.output/"v0_6_f3_b_tail_zoom",True)
    bars(table,("first_scheduled_success_probability",
         "scheduled_attempts_per_rescued_packet","scheduled_attempts_per_packet"),
         ("First scheduled success","Attempts/rescued packet","Attempts/packet"),
         args.output/"v0_6_f3_c_harq_state",("H3","F3"))
    bars(table,("mean_queue","mean_wait","fraction_at_cap2","scheduled_prb_slots"),
         ("Mean queue","Mean wait","Fraction at Cap2","Scheduled PRB-slots"),
         args.output/"v0_6_f3_d_queue_amplification",("H3","F3"))
    bars(table,("p95_latency","p99_latency"),("P95","P99"),
         args.output/"v0_6_f3_e_tail_summary",("H3","F3"))
    bars(table,("p99_latency","scheduled_attempts_per_packet","mean_queue"),
         ("P99","Scheduled attempts/packet","Mean queue"),
         args.output/"v0_6_f3_f_context",("B","H2","F2","H3","F3"))
    hist=[]
    for scheme in ("H3","F3"):
        values=[]
        for seed in SEEDS:
            counts=Counter(int(r["rlc_retx_count"]) for r in all_runs[(scheme,seed)][2])
            values.append([counts[i]/2500 for i in range(7)])
        array=np.asarray(values)
        hist += [{"scheme":scheme,"rlc_retx_count":i,
                  "mean_fraction":array[:,i].mean(),
                  "ci95_half_width":T95_7*array[:,i].std(ddof=1)/math.sqrt(8)}
                 for i in range(7)]
    write_csv(args.output/"rlc_retry_distribution.csv",hist)
    maxima=[]
    for scheme in ("H3","F3"):
        for seed in SEEDS:
            packets=all_runs[(scheme,seed)][2]
            packet=max((r for r in packets if truth(r["delivered_correctly"])),
                       key=lambda r:float(r["completion_latency_slots"]))
            history=[r for r in all_runs[(scheme,seed)][3]
                     if r["payload_id"]==packet["payload_id"]]
            maxima.append({"scheme":scheme,"seed":seed,
                "payload_id":packet["payload_id"],
                "max_latency":packet["completion_latency_slots"],
                "rlc_retx_count":packet["rlc_retx_count"],
                "attempt_history_json":json.dumps([{
                    "slot":int(r["actual_tx_slot"]),"rv":int(r["rv"]),
                    "access":r["access"],"episode":r["harq_episode_id"],
                    "ack":truth(r["ack"])} for r in history])})
    write_csv(args.output/"max_latency_histories.csv",maxima)
    means=[{"scheme":scheme,**{metric:statistics.fmean(float(r[metric])
        for r in table if r["scheme"]==scheme) for metric in table[0]
        if metric not in {"scheme","seed"}}} for scheme in SCENARIOS]
    write_csv(args.output/"scheme_seed_means.csv",means)
    artifacts=sorted(p for p in args.output.iterdir() if p.is_file())
    manifest={"execution_classification":"V0_6-F3-COMPLETE",
        "scientific_classification":args.classification,
        "rho070_decision":args.rho070_decision,
        "implementation_sha":args.implementation_sha,
        "results_sha":args.results_sha,"config_sha256":sha256(args.config),
        "seeds":list(SEEDS),"schemes":{"B":"8 reused","H2":"8 reused",
        "F2":"8 reused","H3":"8 reused","F3":"8 new"},
        "primary_comparison":"H3-F3",
        "statistics":"paired seed-level Student-t 95% CI; exact sign-flip; leave-one-out",
        "artifact_sha256":{p.name:sha256(p) for p in artifacts}}
    (args.output/"analysis_manifest.json").write_text(
        json.dumps(manifest,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"execution":"V0_6-F3-COMPLETE",
        "classification":args.classification,
        "rho070_decision":args.rho070_decision},sort_keys=True))


if __name__=="__main__": main()
