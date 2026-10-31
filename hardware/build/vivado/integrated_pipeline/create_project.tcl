encoding system utf-8
# V4 reproducible build. Relative to this script, never the launch directory.
set script_dir [file normalize [file dirname [info script]]]
set repo_root [file normalize [file join $script_dir .. .. .. ..]]
set ip_dir [file join $repo_root outputs hls_integrated_pipeline hls impl ip]
set out_dir [file join $repo_root outputs vivado_integrated_pipeline]
set stage bit
if {$argc > 0} {set ip_dir [file normalize [lindex $argv 0]]}
if {$argc > 1} {set out_dir [file normalize [lindex $argv 1]]}
if {$argc > 2} {set stage [lindex $argv 2]}
if {$stage ni {validate bit}} {error "stage must be validate or bit"}
if {![file isfile [file join $ip_dir component.xml]]} {error "Export HLS IP first: $ip_dir"}
if {[file exists [file join $out_dir sobel_v3_param.xpr]]} {error "Use a fresh output directory to preserve prior builds: $out_dir"}
set bd_name integrated_pipeline_system
set bd_source [file join $repo_root hardware build vivado sobel_v3_param ${bd_name}.bd]
# The archived BD's gen_directory is relative to this project name.
create_project sobel_v3_param $out_dir -part xc7z020clg400-1
set_property ip_repo_paths $ip_dir [current_project]
update_ip_catalog
if {[llength [get_ipdefs -all xilinx.com:hls:sobel_v4_pipeline:1.0]] == 0} {error "V4 HLS IP not registered"}
set bd_dir [file join $out_dir sobel_v3_param.srcs sources_1 bd $bd_name]
file mkdir $bd_dir
set bd_file [file join $bd_dir ${bd_name}.bd]
file copy $bd_source $bd_file
add_files -norecurse $bd_file
open_bd_design [get_files $bd_file]
validate_bd_design
save_bd_design
if {$stage eq "validate"} {puts "AGRIVISION_V4_BD_VALIDATE_PASS"; close_project; exit 0}
generate_target all [get_files $bd_file]
set wrappers [make_wrapper -files [get_files $bd_file] -top]
add_files -norecurse $wrappers
set_property top ${bd_name}_wrapper [current_fileset]
update_compile_order -fileset sources_1
launch_runs synth_1 -jobs 4
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {error "Synthesis failed"}
launch_runs impl_1 -to_step write_bitstream -jobs 4
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {error "Implementation failed"}
open_run impl_1
report_utilization -file [file join $out_dir utilization.rpt]
report_timing_summary -file [file join $out_dir timing.rpt]
report_drc -file [file join $out_dir drc.rpt]
set bit_source [file join $out_dir sobel_v3_param.runs impl_1 ${bd_name}_wrapper.bit]
if {![file isfile $bit_source]} {error "Missing generated bitstream"}
file copy $bit_source [file join $out_dir agrivision_v4.bit]
write_hw_platform -fixed -include_bit -force [file join $out_dir agrivision_v4.xsa]
set hwh_source [file join $out_dir sobel_v3_param.gen sources_1 bd $bd_name hw_handoff ${bd_name}.hwh]
if {![file isfile $hwh_source]} {error "Missing HWH: $hwh_source"}
file copy $hwh_source [file join $out_dir agrivision_v4.hwh]
puts "AGRIVISION_V4_BUILD_PASS"
close_project
exit 0
