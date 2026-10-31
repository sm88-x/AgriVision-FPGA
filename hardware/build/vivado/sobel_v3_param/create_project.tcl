encoding system utf-8
# Parameterized Sobel reproduction; use tools/build.py hls then tools/build.py vivado.
# Arguments: HLS_IP_DIR OUTPUT_DIR [validate|bit]. Existing projects are preserved.
set script_dir [file normalize [file dirname [info script]]]
set repo_root  [file normalize [file join $script_dir .. .. .. ..]]
set hls_ip_dir [file normalize \
    [file join $repo_root outputs hls_parameterized hls impl ip]]
set proj_dir   [file normalize [file join $repo_root outputs vivado_parameterized]]
if {$argc >= 1} { set hls_ip_dir [file normalize [lindex $argv 0]] }
if {$argc >= 2} { set proj_dir   [file normalize [lindex $argv 1]] }
set stage bit
if {$argc >= 3} {set stage [lindex $argv 2]}
if {$stage ni {validate bit}} {error "stage must be validate or bit"}
if {[file exists [file join $proj_dir sobel_v3_param.xpr]]} {error "Use a fresh output directory: $proj_dir"}

# 工程名必须与原始工程一致:BD 内 gen_directory 相对路径依赖它
set proj_name  sobel_v3_param
set bd_name    parameterized_edge_system
set top_name   parameterized_edge_system_wrapper
set bd_src     [file join $script_dir parameterized_edge_system.bd]
set bit_out    parameterized_edge_system_wrapper.bit
set xsa_out    parameterized_edge.xsa

puts "===== 重建参数 ====="
if {[catch {version -short} vivado_ver]} { set vivado_ver "未知" }
puts "Vivado    : $vivado_ver"
puts "repo_root : $repo_root"
puts "hls_ip_dir: $hls_ip_dir"
puts "proj_dir  : $proj_dir"
puts "proj_name : ${proj_name} (BD 相对路径依赖,必须与原始工程一致)"

# ---- 前置检查 ----
if {![file isfile [file join $hls_ip_dir component.xml]]} {
    puts "ERROR: $hls_ip_dir 下没有 component.xml。"
    puts "  请先执行 Step 0 重导 HLS IP(命令见本文件头部注释),"
    puts "  或用 -tclargs <HLS_IP_DIR> 传入正确的 IP 仓库路径。"
    exit 1
}
if {![file isfile $bd_src]} {
    puts "ERROR: 找不到入库 BD: $bd_src"
    exit 1
}
if {[file normalize $proj_dir] eq $script_dir} {
    puts "ERROR: PROJ_DIR 不能是脚本所在目录(现有工程),请保持默认独立目录。"
    exit 1
}

# ---- 建工程 ----
create_project $proj_name $proj_dir -part xc7z020clg400-1
if {[catch {
    set_property board_part tul.com.tw:pynq-z2:part0:1.0 [current_project]
} err]} {
    puts "WARN: 设置 board_part 失败($err),不影响重建(器件已由 -part 指定)。"
}
set_property ip_repo_paths $hls_ip_dir [current_project]
update_ip_catalog

# ---- 放入 BD(复刻原工程布局:<proj>.srcs/sources_1/bd/<BD名>/<BD名>.bd)----
set bd_dst_dir [file join $proj_dir $proj_name.srcs sources_1 bd $bd_name]
file mkdir $bd_dst_dir
set bd_dst [file join $bd_dst_dir $bd_name.bd]
file copy -force $bd_src $bd_dst
add_files -norecurse $bd_dst

# ---- 打开 BD 并做合法性检查(标准流程第 6 步;batch 模式需显式 open)----
open_bd_design [get_files $bd_dst]
validate_bd_design
save_bd_design
if {$stage eq "validate"} {puts "AGRIVISION_PARAMETERIZED_BD_VALIDATE_PASS"; close_project; exit 0}

# ---- 生成 IP 输出产物(重建全部 .xci,含 sobel_v3 HLS IP)----
generate_target all [get_files $bd_dst]

# ---- wrapper 作顶层 ----
set wrapper [make_wrapper -files [get_files $bd_dst] -top]
add_files -norecurse $wrapper
set_property top $top_name [current_fileset]
update_compile_order -fileset sources_1

# ---- 综合 + 实现 + 生成 bit(-jobs 按机器核数调整)----
launch_runs synth_1 -jobs 8
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {error "Synthesis failed"}
launch_runs impl_1 -to_step write_bitstream -jobs 8
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {error "Implementation failed"}

# ---- 交付物放到重建目录(顶层名=交付名,无需改名;不覆盖仓库现有文件)----
set bit_src [file join $proj_dir $proj_name.runs impl_1 $bit_out]
if {![file isfile $bit_src]} {
    puts "ERROR: 未生成 $bit_src ,请检查 impl_1 日志。"
    exit 1
}
file copy -force $bit_src [file join $proj_dir parameterized_edge.bit]

if {[catch {
    write_hw_platform -fixed -include_bit -force [file join $proj_dir $xsa_out]
} err]} {
    puts "WARN: write_hw_platform 失败($err)。"
    puts "      可改用 GUI:File -> Export Hardware(Include bitstream),输出到 $proj_dir"
}

puts ""
puts "===== 完成 ====="
puts "bit : $proj_dir/$bit_out"
puts "xsa : $proj_dir/${xsa_out} (若导出成功)"
puts ""
set hwh_src [file join $proj_dir sobel_v3_param.gen sources_1 bd $bd_name hw_handoff ${bd_name}.hwh]
if {![file isfile $hwh_src]} {error "Missing HWH: $hwh_src"}
file copy -force $hwh_src [file join $proj_dir parameterized_edge.hwh]
open_run impl_1
report_utilization -file [file join $proj_dir utilization.rpt]
report_timing_summary -file [file join $proj_dir timing.rpt]
puts "AGRIVISION_PARAMETERIZED_BUILD_PASS"
close_project
exit 0
