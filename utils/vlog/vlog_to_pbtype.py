#!/usr/bin/env python3
"""\
Convert a Verilog simulation model to a VPR `pb_type.xml`

The following are allowed on a top level module:

    - `(* blackbox *)` : specify that the module has no interconnect or child
    pb_types (but if modes are used then its modes are allowed to have these).
    This will also set the BLIF model to be `.subckt <name>` unless CLASS is
    also specified.

    - `(* CLASS="lut|routing|mux|flipflop|mem" *)` : specify the class of an given
    instance.

    - `(* MODES="mode1; mode2; ..." *)` : specify that the module has more than one functional
    mode, each with a given name. The module will be evaluated n times, each time setting
    the MODE parameter to the nth value in the list of mode names. Each evaluation will be
    put in a pb_type `<mode>` section named accordingly.

    - `(* MODEL_NAME="model" *)` : override the name used for <model> and for
    ".subckt name" in the BLIF model. Mostly intended for use with w.py, when several
    different pb_types implement the same model.

The following are allowed on nets within modules (TODO: use proper Verilog timing):
    - `(* SETUP="clk 10e-12" *)` : specify setup time for a given clock

    - `(* HOLD="clk 10e-12" *)` : specify hold time for a given clock

    - `(* CLK_TO_Q="clk 10e-12" *)` : specify clock-to-output time for a given clock

    - `(* DELAY_CONST_{input}="30e-12" *)` : specify a constant max delay from an input (applied to the output)

    - `(* DELAY_MATRIX_{input}="30e-12 35e-12; 20e-12 25e-12; ..." *)` : specify a VPR
        delay matrix (semicolons indicate rows). In this format columns specify
        inputs bits and rows specify output bits. This should be applied to the output.

The following are allowed on ports:
    - `(* CLOCK *)` : force a given port to be a clock

    - `(* ASSOC_CLOCK="RDCLK" *)` : force a port's associated clock to a given value

    - `(* PORT_CLASS="clock" *)` : specify the VPR "port_class"

The Verilog define "PB_TYPE" is set during generation.
"""

import os, sys
import argparse, re

import lxml.etree as ET

import yosys.run
from yosys.json import YosysJSON

sys.path.insert(0, "..")
from lib import xmlinc
from sdf_timing import sdfparse


def make_timings(pb_type_xml, sdf_file_name, sdf_cell_name=None, sdf_inst_name=None, sdf_variant="slow"):
    """
    Loads timings from a given SDF file and converts them to appropriate
    statements within pb_type XML.

    If sdf_cell_name is None then the function tries to find in SDF cell with
    name equal to the pb_type name.

    If sdf_inst_name is None and there is only one instance of that cell in SDF
    then that one is taken.
    """

    def parse_timescale(timescale_str):
        """
        Parses a timescale expression and returns its numerical value
        """
        match = re.match("([0-9]+)(fs|ps|ns|us|ms|s?)$", timescale_str)

        base = float(match.group(1))
        suffix = match.group(2)

        if suffix == "fs":
            base *= 1e-15
        elif suffix == "ps":
            base *= 1e-12
        elif suffix == "ns":
            base *= 1e-9
        elif suffix == "us":
            base *= 1e-6
        elif suffix == "ms":
            base *= 1e-3

        return base

    def pin_list(xml_tag):
        """
        Returns a pin list generated from <pb_type> port definition. When the
        num_pb > 1 ports are named as nameX where X is the pin index.
        """
        name = xml_tag.get("name")
        numb = int(xml_tag.get("num_pins"))
        if numb == 1:
            return [name]
        else:
            return ["{}{}".format(name, i) for i in range(numb)]

    # Load the SDF
    with open(sdf_file_name, "r") as fp:
        sdf = sdfparse.parse(fp.read())

    # Get timescale
    scale = parse_timescale(sdf["header"]["timescale"])

    # Get pb name
    pb_name = pb_type_xml.get("name")

    # Get pb ports (inputs, clocks, outputs)
    pb_inputs = []
    for xml_tag in pb_type_xml.findall("input"):
        pb_inputs.extend(pin_list(xml_tag))
    pb_clocks = []
    for xml_tag in pb_type_xml.findall("clock"):
        pb_clocks.extend(pin_list(xml_tag))
    pb_outputs = []
    for xml_tag in pb_type_xml.findall("output"):
        pb_outputs.extend(pin_list(xml_tag))

    # Try automatically matching the pb_name to SDF cell name
    if sdf_cell_name is None:
        sdf_cell_name = pb_name.upper()

    if sdf_cell_name not in sdf["cells"].keys():
        print("ERROR, cell name '{}' not found in the SDF file!".format(sdf_cell_name))
        return pb_type_xml

    # Get the SDF timing info for a particular CELL/INSTANCE
    try:
        sdf_cell = sdf["cells"][sdf_cell_name]
    except KeyError:
        print("ERROR, the SDF file does not contain data for cell '{}'".format(sdf_cell_name))
        exit(-1)

    # If the instance is not specified and there is only one in the SDF than
    # take this one
    if sdf_inst_name is None:

        if len(sdf_cell.keys()) > 1:
            print("ERROR, multiple instances for cell '{}' and instance not given".format(sdf_cell_name))
            exit(-1)

        sdf_inst_name = next(iter(sdf_cell.keys()), None)

    try:
        sdf_timing = sdf_cell[sdf_inst_name]
    except KeyError:
        print("ERROR, the SDF file does not contain data for cell instance '{}'".format(sdf_inst_name))
        exit(-1)

    # Process SDF timing entires
    for key, sdf_timing in sdf_timing.items():

        sdf_inp = sdf_timing["from_pin"]
        sdf_out = sdf_timing["to_pin"]

        assert sdf_timing["is_incremental"] == False
        assert sdf_timing["is_cond"] == False

        # IOPATH delay
        if sdf_timing["type"] == "iopath":

            assert sdf_timing["is_absolute"] == True

            # Find pins
            pb_inp = sdf_inp if sdf_inp in pb_inputs else None
            pb_clk = sdf_inp if sdf_inp in pb_clocks else None
            pb_out = sdf_out if sdf_out in pb_outputs else None

            # Cannot match SDF to <pb_type>
            if (pb_inp is None and pb_clk is None) or pb_out is None:
                print("Cannot match pins ({} -> {}) for SDF timing entry:".format(sdf_inp, sdf_out))
                print(" ", sdf_timing)
                continue

            # "delay_constant"
            if pb_clk is None:
                xml_tag = ET.SubElement(pb_type_xml, "delay_constant")
                xml_tag.set("in_port",  "{}.{}".format(pb_name, pb_inp))
                xml_tag.set("out_port", "{}.{}".format(pb_name, pb_out))
                for var in ("min", "max"):
                    xml_tag.set(var, "{:.3e}".format(sdf_timing["delay_paths"][sdf_variant][var] * scale))

            # "T_clock_to_Q"
            else:
                xml_tag = ET.SubElement(pb_type_xml, "T_clock_to_Q")
                xml_tag.set("clock",  "{}.{}".format(pb_name, pb_clk))
                xml_tag.set("port", "{}.{}".format(pb_name, pb_out))
                for var in ("min", "max"):
                    xml_tag.set(var, "{:.3e}".format(sdf_timing["delay_paths"][sdf_variant][var] * scale))

        # SETUP / HOLD delay
        elif sdf_timing["type"] in ("setup", "hold"):

            assert sdf_timing["is_absolute"] == False

            # Find pins
            pb_inp = sdf_out if sdf_out in pb_inputs else None
            pb_clk = sdf_inp if sdf_inp in pb_clocks else None

            # Cannot match SDF to <pb_type>
            if pb_inp is None or pb_clk is None:
                print("Cannot match pins ({}, {}) for SDF timing entry:".format(sdf_inp, sdf_out))
                print(" ", sdf_timing)
                continue

            xml_tag = ET.SubElement(pb_type_xml, "T_{}".format(sdf_timing["type"]))
            xml_tag.set("clock", "{}.{}".format(pb_name, pb_clk))
            xml_tag.set("port", "{}.{}".format(pb_name, pb_inp))

            # The VPR supports only one value so the "max" is chosen
            xml_tag.set("value", "{:.3e}".format(sdf_timing["delay_paths"]["nominal"]["max"] * scale))

        # RECOVERY / REMOVAL delay
        elif sdf_timing["type"] in ("recovery", "removal"):
            # Not supported in VPR so ignored
            pass

        # Something else
        else:
            print("ERROR, unsupported timing type '{}'".format(sdf_timing["type"]))
            print(" ", sdf_timing)

    return pb_type_xml


INVALID_INSTANCE = -1


def is_mod_blackbox(mod):
    """ Returns true if module is annotated with blackbox (or equivilant).

    Yosys supports 3 attributes that denote blackbox behavior:

    "blackbox" - Blackbox with no internal wiring
    "whitebox" - Blackbox with internal connections and timing.
    "lib_whitebox" - Like "whitebox" when read with "-lib", otherwise
        attribute is removed.

    """

    return (mod.attr("lib_whitebox", 0) == 1) or \
           (mod.attr("whitebox", 0) == 1) or \
           (mod.attr("blackbox", 0) == 1)


def mod_pb_name(mod):
    """Convert a Verilog module to a pb_type name in the format documented here:
    https://github.com/SymbiFlow/symbiflow-arch-defs/#names"""

    modes = mod.attr("MODES", None)
    has_modes = modes is not None
    # Process type and class of module
    mod_cls = mod.CLASS
    if mod_cls == "routing":
        return mod.name
    elif mod_cls == "mux":
        return mod.name
    elif mod_cls == "flipflop":
        return mod.name
    elif mod_cls == "lut":
        return mod.name
    elif is_mod_blackbox(mod) and not has_modes:
        return mod.name
    else:
        #TODO: other types
        return mod.name


def strip_name(name):
    if '\\' in name:
        ts = name.find('\\')
        tf = name.rfind('\\')
        return name[ts + 1:tf]
    return name


def make_pb_content(yj, mod, xml_parent, mod_pname, is_submode=False):
    """Build the pb_type content - child pb_types, timing and direct interconnect,
    but not IO. This may be put directly inside <pb_type>, or inside <mode>."""

    def get_module_name(pin, instance=INVALID_INSTANCE):
        """Returns the name of the module relative to the pin and a boolean that indicates whether
        the module is a cell (True) or the top one (False)"""
        if instance <= INVALID_INSTANCE:
            instance = ""
        else:
            instance = "[" + str(instance) + "]"
        cname, cellpin = pin
        if cname.startswith("$"):
            return mod.cell_type(cname) + instance, True
        elif cname != mod.name:
            cname = mod.cell_type(cname)
            modname = mod_pb_name(yj.module(cname)) + instance
            return modname, True
        else:
            return mod_pname, False

    def get_cellpin(pin):
        cname, cellpin = pin
        return cellpin

    def create_port(pin_name, mod_name, is_cell, direction):
        """Returns a dictionary containing the port definition. If the module is a cell, the port
        contains the 'from' attribute."""
        port = dict()
        port['name'] = pin_name
        port['type'] = direction

        if is_cell:
            port['from'] = mod_name

        return port

    def make_direct_conn(
            ic_xml,
            src,
            dst,
            source_instance=INVALID_INSTANCE,
            dest_instance=INVALID_INSTANCE
    ):
        s_cellpin = get_cellpin(src)
        d_cellpin = get_cellpin(dst)
        s_cname, s_is_cell = get_module_name(src, source_instance)
        d_cname, d_is_cell = get_module_name(dst, dest_instance)

        s_port = create_port(s_cellpin, s_cname, s_is_cell, "input")
        d_port = create_port(d_cellpin, d_cname, d_is_cell, "output")

        dir_xml = ET.SubElement(ic_xml, 'direct')

        s_port_xml = ET.SubElement(dir_xml, 'port', s_port)
        d_port_xml = ET.SubElement(dir_xml, 'port', d_port)

    # Find out whether or not the module we are generating content for is a blackbox
    is_blackbox = is_mod_blackbox(mod) or not mod.cells

    # List of entries in format ((from_cell, from_pin), (to_cell, to_pin))
    interconn = []

    # Determine multiple instances of the same cell:
    cells = dict()
    for cname, i_of in mod.cells:
        if i_of in cells:
            cells[i_of]['count'] += 1
            cells[i_of]['is_multi_instance'] = True
            # assign unique instance number
            cells[i_of][cname] = cells[i_of]['count']
        else:
            cells[i_of] = dict()
            cells[i_of]['is_multi_instance'] = False
            cells[i_of]['count'] = 0
            cells[i_of][cname] = 0

    # Blackbox modules don't have inner cells or interconnect (but do still have timing)
    if (not is_blackbox) or is_submode:
        # Process cells. First build the list of cnames.
        processed_cells = list()
        for cname, i_of in mod.cells:
            pb_name = strip_name(i_of)
            pbtype_already_included = False
            if i_of in processed_cells:
                # do not emit xml include for every instance of multi instace cell
                pbtype_already_included = True
            else:
                processed_cells.append(i_of)
            instance = INVALID_INSTANCE

            # If currently considered cell is a multi instance one, pass it's unique
            # instance number to connection creator. If not, pass INVALID_INSTANCE
            # constant
            if cells[i_of]['is_multi_instance']:
                instance = cells[i_of][cname]
            module_file = yj.get_module_file(i_of)
            module_path = os.path.dirname(module_file)
            module_basename = os.path.basename(module_file)

            # Heuristic for autogenerated files from w.py
            if not pbtype_already_included:
                wm = re.match(r"([A-Za-z0-9_]+)\.sim\.v", module_basename)
                if wm:
                    pb_type_path = "{}/{}.pb_type.xml".format(
                        module_path,
                        wm.group(1).lower()
                    )
                else:
                    pb_type_path = "{}/pb_type.xml".format(module_path)

                # inlude contents of the included pb_type, but update it's
                # num_pb value
                with open(pb_type_path, 'r') as inc_xml:
                    xml_inc = ET.fromstring(inc_xml.read().encode('utf-8'))
                    inc_attrib = xml_inc.attrib
                    inc_attrib['num_pb'] = str(cells[i_of]['count'] + 1)

                inc_pb_type = ET.SubElement(xml_parent, 'pb_type', inc_attrib)
                xmlinc.include_xml(
                    parent=inc_pb_type,
                    href=pb_type_path,
                    outfile=outfile,
                    xptr="xpointer(pb_type/child::node())"
                )

            # In order to avoid overspecifying interconnect, there are two directions we currently
            # consider. All interconnect going INTO a cell, and interconnect going out of a cell
            # into a top level output - or all outputs if "mode" is used.
            inp_cons = mod.cell_conns(cname, "input")
            for pin, net in inp_cons:
                drvs = mod.net_drivers(net)
                assert len(drvs) > 0, (
                    "ERROR: pin {}.{} has no driver, interconnect will be missing\n{}"
                    .format(pb_name, pin, mod)
                )
                assert len(drvs) < 2, (
                    "ERROR: pin {}.{} has multiple drivers, interconnect will be overspecified"
                    .format(pb_name, pin)
                )
                for drv_cell, drv_pin in drvs:
                    print(pin, net, drv_cell, drv_pin)
                    # check if we're driven by multi instance cell
                    drive_instance = INVALID_INSTANCE
                    drv_cell_type = [
                        c[1] for c in mod.cells if c[0] == drv_cell
                    ]
                    if len(drv_cell_type) != 0:
                        drv_cell_type = drv_cell_type[0]
                        if cells[drv_cell_type]['is_multi_instance']:
                            # get drv_cell unique instance number
                            drive_instance = cells[drv_cell_type][drv_cell]
                    interconn.append(
                        (
                            (drv_cell, drv_pin), (cname, pin), drive_instance,
                            instance
                        )
                    )

            out_cons = mod.cell_conns(cname, "output")
            for pin, net in out_cons:
                sinks = mod.net_sinks(net)
                for sink_cell, sink_pin in sinks:
                    if sink_cell != mod.name:
                        continue
                    # Only consider outputs from cell to top level IO. Inputs to other cells will be dealt with
                    # in those cells.
                    interconn.append(
                        (
                            (cname, pin), (sink_cell, sink_pin), instance,
                            INVALID_INSTANCE
                        )
                    )

        # Direct pin->pin connections
        for net in mod.nets:
            drv = mod.conn_io(net, "input")
            if not drv:
                continue
            assert len(drv) == 1, (
                "ERROR: net {} has multiple drivers {}, interconnect will be over specified"
                .format(net, drv)
            )
            for snk in mod.conn_io(net, "output"):
                conn = ((mod.name, drv[0]), (mod.name, snk))
                interconn.append(conn)

        ic_xml = ET.SubElement(xml_parent, "interconnect")
        # Process interconnect
        for source, dest, src_instance, dst_instance in interconn:
            make_direct_conn(ic_xml, source, dest, src_instance, dst_instance)

    def process_clocked_tmg(tmgspec, port, xmltype, xml_parent):
        """Add a suitable timing spec if necessary to the pb_type"""
        if tmgspec is not None:
            splitspec = tmgspec.split(" ")
            assert len(
                splitspec
            ) == 2, 'bad timing specification "{}", must be of format "clock value"'.format(
                tmgspec
            )
            attrs = {"port": port, "clock": splitspec[0]}
            if xmltype == "T_clock_to_Q":
                attrs["max"] = splitspec[1]
            else:
                attrs["value"] = splitspec[1]
            ET.SubElement(xml_parent, xmltype, attrs)

    # Process timing
    for name, width, bits, iodir in mod.ports:
        port = "{}".format(name)
        # Clocked timing
        Tsetup = mod.net_attr(name, "SETUP")
        Thold = mod.net_attr(name, "HOLD")
        Tctoq = mod.net_attr(name, "CLK_TO_Q")
        process_clocked_tmg(Tsetup, port, "T_setup", xml_parent)
        process_clocked_tmg(Thold, port, "T_hold", xml_parent)
        process_clocked_tmg(Tctoq, port, "T_clock_to_Q", xml_parent)

        # Combinational delays
        dly_prefix = "DELAY_CONST_"
        dly_mat_prefix = "DELAY_MATRIX_"
        for attr, atvalue in sorted(mod.net_attrs(name).items()):
            if attr.startswith(dly_prefix):
                # Single, constant delays
                inp = attr[len(dly_prefix):]
                inport = "{}".format(inp)
                ET.SubElement(
                    xml_parent, "delay_constant", {
                        "in_port": inport,
                        "out_port": port,
                        "max": str(atvalue)
                    }
                )
            elif attr.startswith(dly_mat_prefix):
                # Constant delay matrices
                inp = attr[len(dly_mat_prefix):]
                inport = "{}".format(inp)
                mat = "\n" + atvalue.replace(";", "\n") + "\n"
                xml_mat = ET.SubElement(
                    xml_parent, "delay_matrix", {
                        "in_port": inport,
                        "out_port": port,
                        "type": "max"
                    }
                )
                xml_mat.text = mat


def make_pb_type(yj, mod):
    """Build the pb_type for a given module. mod is the YosysModule object to
    generate."""

    modes = mod.attr("MODES", None)
    if modes is not None:
        modes = modes.split(";")
    mod_pname = mod_pb_name(mod)

    pb_xml_attrs = dict()
    pb_xml_attrs["name"] = mod_pname
    # If we are a blackbox with no modes, then generate a blif_model
    is_blackbox = is_mod_blackbox(mod) or not mod.cells
    has_modes = modes is not None

    print("is_blackbox", is_blackbox, "has_modes?", has_modes)

    # Process type and class of module
    mod_cls = mod.CLASS
    if mod_cls is not None:
        if mod_cls == "lut":
            pb_xml_attrs["blif_model"] = ".names"
            pb_xml_attrs["class"] = "lut"
        elif mod_cls == "routing":
            # TODO: pb_xml_attrs["class"] = "routing"
            pass
        elif mod_cls == "mux":
            # TODO: ?
            pass
        elif mod_cls == "flipflop":
            pb_xml_attrs["blif_model"] = ".latch"
            pb_xml_attrs["class"] = "flipflop"
        else:
            assert False, "unknown class {}".format(mod_cls)
    elif is_blackbox and not has_modes:
        pb_xml_attrs["blif_model"
                     ] = ".subckt " + mod.attr("MODEL_NAME", mod.name)

    # set num_pb to 1, it will be updated if this pb_type
    # will be included by another one
    pb_xml_attrs["num_pb"] = "1"
    pb_type_xml = ET.Element(
        "pb_type", pb_xml_attrs, nsmap={'xi': xmlinc.xi_url}
    )
    # Process IOs
    clocks = yosys.run.list_clocks(args.infiles, mod.name)
    for name, width, bits, iodir in mod.ports:
        ioattrs = {"name": name, "num_pins": str(width)}
        pclass = mod.net_attr(name, "PORT_CLASS")
        if pclass is not None:
            ioattrs["port_class"] = pclass
        if name in clocks:
            ET.SubElement(pb_type_xml, "clock", ioattrs)
        elif iodir == "input":
            ET.SubElement(pb_type_xml, "input", ioattrs)
        elif iodir == "output":
            ET.SubElement(pb_type_xml, "output", ioattrs)
        else:
            assert False, "bidirectional ports not supported in VPR pb_types"

    if has_modes:
        for mode in modes:
            smode = mode.strip()
            mode_xml = ET.SubElement(pb_type_xml, "mode", {"name": smode})
            # Rerun Yosys with mode parameter
            mode_yj = YosysJSON(
                yosys.run.vlog_to_json(
                    args.infiles,
                    flatten=False,
                    aig=False,
                    mode=smode,
                    module_with_mode=mod.name
                )
            )
            mode_mod = mode_yj.module(mod.name)
            make_pb_content(yj, mode_mod, mode_xml, mod_pname, True)
    else:
        make_pb_content(yj, mod, pb_type_xml, mod_pname)

    return pb_type_xml


parser = argparse.ArgumentParser(
    description=__doc__.strip(), formatter_class=argparse.RawTextHelpFormatter
)
parser.add_argument(
    'infiles',
    metavar='input.v',
    type=str,
    nargs='+',
    help="""\
One or more Verilog input files, that will be passed to Yosys internally.
They should be enough to generate a flattened representation of the model,
so that paths through the model can be determined.
"""
)
parser.add_argument(
    '--top',
    help="""\
Top level module, will usually be automatically determined from the file name
%.sim.v
"""
)
parser.add_argument(
    '--outfile',
    '-o',
    type=argparse.FileType('w'),
    default="pb_type.xml",
    help="""\
Output filename, default 'model.xml'
"""
)

parser.add_argument(
    '--includes',
    help="""\
Command seperate list of include directories.
""",
    default=""
)

parser.add_argument(
    '--sdf',
    type=str,
    default=None,
    help="""SDF file name for timing import"""
)

parser.add_argument(
    '--sdf-cell',
    type=str,
    default=None,
    help="""SDF cell type to import timing from. If not given then inferred automatically"""
)

parser.add_argument(
    '--sdf-instance',
    type=str,
    default=None,
    help="""SDF cell instance to import timing from. If not given then inferred automatically"""
)

def main(args):

    iname = os.path.basename(args.infiles[0])

    yosys.run.add_define("PB_TYPE")
    vjson = yosys.run.vlog_to_json(args.infiles, flatten=False, aig=False)
    yj = YosysJSON(vjson)

    if args.top is not None:
        top = args.top
    else:
        wm = re.match(r"([A-Za-z0-9_]+)\.sim\.v", iname)
        if wm:
            top = wm.group(1).upper()
        else:
            print(
                "ERROR file name not of format %.sim.v ({}), cannot detect top level. Manually specify the top level module using --top"
                .format(iname)
            )
            sys.exit(1)

    tmod = yj.module(top)

    pb_type_xml = make_pb_type(yj, tmod)

    # Inject timings
    if args.sdf is not None:
        pb_type_xml = make_timings(pb_type_xml, args.sdf, args.sdf_cell, args.sdf_instance)

    args.outfile.write(
        ET.tostring(
            pb_type_xml,
            pretty_print=True,
            encoding="utf-8",
            xml_declaration=True
        ).decode('utf-8')
    )
    print("Generated {} from {}".format(args.outfile.name, iname))
    args.outfile.close()


if __name__ == "__main__":
    args = parser.parse_args()
    sys.exit(main(args))
