#!/usr/bin/env python3
"""gen_vhtb.py <taskdir> -- build the VHDL equivalence testbench of a task from task.json "equiv".

The golden is analysed into library `gold`, the candidate into `work`; tb_equiv instantiates gold.<top> and
work.<top>.  Ports come from equiv.vports: [name, "in"|"out", type, optional gold-side type for record ports].
Stimulus: equiv.stim (VHDL statements) runs once per cycle right after the falling edge of the clock (or of the
pacing clock when the design has none); helpers rv(n) (random slv), rb (random bit), chance(n) (n/256), cyc.
Outputs are compared 1 ns before every rising edge; a golden bit that is not '0'/'1' is not checked.
"""
import json, os, sys


def to_slv(expr, typ):
    t = typ.lower().replace(" ", "")
    if t == "std_logic":
        return "std_logic_vector'(0 => %s)" % expr
    if t.startswith("std_logic_vector"):
        return expr
    if t.startswith("signed") or t.startswith("unsigned"):
        return "std_logic_vector(%s)" % expr
    raise SystemExit("unsupported type " + typ)


def main():
    td = os.path.abspath(sys.argv[1])
    cfg = json.load(open(os.path.join(td, "task.json")))
    eq = cfg["equiv"]
    top = cfg["top"]
    ports = eq["vports"]
    clk = eq.get("clock")
    p = []
    p.append("library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all; use ieee.math_real.all;")
    p.append(eq.get("uses", ""))
    p.append("library gold;")
    p.append("entity tb_equiv is generic (SEED : integer := 1; CYCLES : integer := %d); end entity;" % eq.get("cycles", 4000))
    p.append("architecture sim of tb_equiv is")
    p.append("    signal tclk : std_logic := '0';")
    p.append("    signal done : boolean := false;")
    p.append("    signal cyc  : integer := 0;")
    for pt in ports:
        n, d, t = pt[0], pt[1], pt[2]
        if d == "in":
            init = eq.get("init_values", {}).get(n)
            if init is None:
                init = "'0'" if t.lower() == "std_logic" else "(others => '0')"
            if n == clk:
                continue
            p.append("    signal %s : %s := %s;" % (n, t, init))
            if len(pt) > 3:
                p.append("    signal g_%s : %s;" % (n, pt[3]))
        else:
            p.append("    signal g_%s, c_%s : %s;" % (n, n, t))
    p.append("    function mism(g, c : std_logic_vector) return integer is")
    p.append("        variable k : integer := 0; variable cc : std_logic_vector(g'range) := c;")
    p.append("    begin")
    p.append("        for i in g'range loop")
    p.append("            if g(i) = '0' or g(i) = '1' then")
    p.append("                if cc(i) /= g(i) then return -1; end if; k := k + 1;")
    p.append("            end if;")
    p.append("        end loop;")
    p.append("        return k;")
    p.append("    end function;")
    p.append("begin")
    p.append("    tclk <= not tclk after 5 ns when not done;")
    p.append(eq.get("conc", ""))
    for inst, lib in (("g", "gold"), ("c", "work")):
        conns = []
        for pt in ports:
            n, d = pt[0], pt[1]
            if d == "in":
                src = "tclk" if n == clk else (("g_" + n) if (inst == "g" and len(pt) > 3) else n)
                conns.append("%s => %s" % (n, src))
            else:
                conns.append("%s => %s_%s" % (n, inst, n))
        p.append("    u_%s : entity %s.%s port map (%s);" % (inst, lib, top, ", ".join(conns)))
    p.append("    stim : process")
    p.append("        variable s1 : positive := SEED; variable s2 : positive := SEED * 7919 + 13; variable rr : real;")
    p.append("        impure function rb return std_logic is begin uniform(s1, s2, rr); if rr < 0.5 then return '0'; else return '1'; end if; end function;")
    p.append("        impure function rv(n : natural) return std_logic_vector is variable v : std_logic_vector(n - 1 downto 0); begin")
    p.append("            for i in v'range loop v(i) := rb; end loop; return v; end function;")
    p.append("        impure function chance(n : natural) return boolean is begin uniform(s1, s2, rr); return rr * 256.0 < real(n); end function;")
    p.append("    begin")
    p.append(eq.get("init", ""))
    p.append("        for k in 0 to CYCLES - 1 loop")
    p.append("            wait until falling_edge(tclk);")
    p.append("            cyc <= k;")
    p.append(eq["stim"])
    p.append("        end loop;")
    p.append("        wait until falling_edge(tclk);")
    p.append("        done <= true; wait;")
    p.append("    end process;")
    p.append("    chk : process")
    p.append("        variable errs, checks, known, r : integer := 0;")
    p.append("    begin")
    p.append("        while not done loop")
    p.append("            wait until falling_edge(tclk) or done;")
    p.append("            exit when done;")
    p.append("            wait for 4 ns;")
    p.append("            checks := checks + 1;")
    for pt in ports:
        n, d, t = pt[0], pt[1], pt[2]
        if d != "out":
            continue
        p.append("            r := mism(%s, %s);" % (to_slv("g_" + n, t), to_slv("c_" + n, t)))
        p.append("            if r < 0 then errs := errs + 1; if errs <= 5 then report \"MISMATCH cyc=\" & integer'image(cyc) & \" port=%s golden=\" & to_hstring(%s) & \" candidate=\" & to_hstring(%s) severity note; end if; else known := known + r; end if;"
                 % (n, to_slv("g_" + n, t), to_slv("c_" + n, t)))
    p.append("            exit when errs >= 20;")
    p.append("        end loop;")
    p.append("        if errs = 0 then report \"EQUIV_PASS seed=\" & integer'image(SEED) & \" checks=\" & integer'image(checks) & \" known_bits=\" & integer'image(known) severity note;")
    p.append("        else report \"EQUIV_FAIL seed=\" & integer'image(SEED) & \" mismatching_checks=\" & integer'image(errs) severity note; end if;")
    p.append("        std.env.finish;")
    p.append("        wait;")
    p.append("    end process;")
    p.append("end architecture;")
    open(os.path.join(td, "tb_equiv.vhd"), "w").write("\n".join(p) + "\n")


if __name__ == "__main__":
    main()
