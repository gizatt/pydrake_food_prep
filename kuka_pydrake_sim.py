# -*- coding: utf8 -*-

import argparse
import random
import time
import os

import matplotlib.pyplot as plt
import numpy as np

import pydrake
from pydrake.all import (
    DiagramBuilder,
    RgbdCamera,
    RigidBodyFrame,
    RigidBodyPlant,
    RigidBodyTree,
    RungeKutta2Integrator,
    Shape,
    SignalLogger,
    Simulator,
)

from underactuated.meshcat_rigid_body_visualizer import (
    MeshcatRigidBodyVisualizer)

import kuka_controllers
import kuka_ik
import kuka_utils

if __name__ == "__main__":
    np.set_printoptions(precision=5, suppress=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("-T", "--duration",
                        type=float,
                        help="Duration to run sim.",
                        default=1000.0)
    parser.add_argument("--test",
                        action="store_true",
                        help="Help out CI by launching a meshcat server for "
                             "the duration of the test.")
    parser.add_argument("-N", "--n_objects",
                        type=int, default=10,
                        help="# of objects to spawn")
    parser.add_argument("--seed",
                        type=float, default=time.time(),
                        help="RNG seed")
    parser.add_argument("--hacky_save_video",
                        action="store_true",
                        help="Saves a video, but does it by screen recording \
                              the area where I usually keep meshcat open.")
    parser.add_argument("--show_plots",
                        action="store_true",
                        help="Shows traces of desired vs achieved trajectories.")
    parser.add_argument("--animate_forever",
                        action="store_true",
                        help="Animates the completed sim in meshcat repeatedly.")

    args = parser.parse_args()
    int_seed = int(args.seed*1000. % 2**32)
    random.seed(int_seed)
    np.random.seed(int_seed)

    meshcat_server_p = None
    if args.test:
        print "Spawning"
        import subprocess
        meshcat_server_p = subprocess.Popen(["meshcat-server"])
    else:
        print "Warning: if you have not yet run meshcat-server in another " \
              "terminal, this will hang."

    # Construct the robot and its environment
    rbt = RigidBodyTree()
    world_builder = kuka_utils.ExperimentWorldBuilder()
    world_builder.setup_kuka(rbt)
    z_table = world_builder.table_top_z_in_world
    rbt_just_kuka = rbt.Clone()
    for k in range(args.n_objects):
        world_builder.add_cut_cylinder_to_tabletop(rbt, "cyl_%d" % k)
    rbt.compile()
    rbt_just_kuka.compile()

    # Figure out initial pose for the arm
    ee_body=rbt_just_kuka.FindBody("right_finger").get_body_index()
    ee_point=np.array([0.0, 0.03, 0.0])
    end_effector_desired = np.array([0.5, 0.0, z_table+0.5, -np.pi/2., 0., 0.])
    q0_kuka_seed = rbt_just_kuka.getZeroConfiguration()
    # "Center low" from IIWA stored_poses.json from Spartan
    # + closed hand
    q0_kuka_seed[0:7] = np.array([-0.18, -1., 0.12, -1.89, 0.1, 1.3, 0.38])
    q0_kuka, info = kuka_ik.plan_ee_configuration(
        rbt_just_kuka, q0_kuka_seed, q0_kuka_seed, end_effector_desired, ee_body,
        ee_point, allow_collision=True, euler_limits=0.01)
    if info != 1:
        print "Info %d on IK for initial posture." % info
    q0 = rbt.getZeroConfiguration()
    q0[0:9] = q0_kuka
    q0 = world_builder.project_rbt_to_nearest_feasible_on_table(
        rbt, q0)

    # Set up a visualizer for the robot
    mrbv = MeshcatRigidBodyVisualizer(rbt, draw_timestep=0.01)
    # (wait while the visualizer warms up and loads in the models)
    time.sleep(2.0)

    # Plan a robot motion to maneuver from the initial posture
    # to a posture that we know should grab the object.
    # (Grasp planning is left as an exercise :))

    # Make our RBT into a plant for simulation
    rbplant = RigidBodyPlant(rbt)
    rbplant.set_name("Rigid Body Plant")

    # Build up our simulation by spawning controllers and loggers
    # and connecting them to our plant.
    builder = DiagramBuilder()
    # The diagram takes ownership of all systems
    # placed into it.
    rbplant_sys = builder.AddSystem(rbplant)

    # Spawn the controller that drives the Kuka to its
    # desired posture.
    kuka_controller = builder.AddSystem(
        kuka_controllers.KukaController(rbt, rbplant_sys))
    builder.Connect(rbplant_sys.state_output_port(),
                    kuka_controller.robot_state_input_port)
    builder.Connect(kuka_controller.get_output_port(0),
                    rbplant_sys.get_input_port(0))

    # Same for the hand
    hand_controller = builder.AddSystem(
        kuka_controllers.HandController(rbt, rbplant_sys))
    builder.Connect(rbplant_sys.state_output_port(),
                    hand_controller.robot_state_input_port)
    builder.Connect(hand_controller.get_output_port(0),
                    rbplant_sys.get_input_port(1))

    # Create a high-level state machine to guide the robot
    # motion...
    manip_state_machine = builder.AddSystem(
        kuka_controllers.ManipStateMachine(
            rbt, rbt_just_kuka, q0[0:7],
            world_builder=world_builder,
            hand_controller=hand_controller,
            kuka_controller=kuka_controller,
            mrbv = mrbv))
    builder.Connect(rbplant_sys.state_output_port(),
                    manip_state_machine.robot_state_input_port)
    builder.Connect(manip_state_machine.hand_setpoint_output_port,
                    hand_controller.setpoint_input_port)
    builder.Connect(manip_state_machine.kuka_setpoint_output_port,
                    kuka_controller.setpoint_input_port)

    # Hook up the visualizer we created earlier.
    visualizer = builder.AddSystem(mrbv)
    builder.Connect(rbplant_sys.state_output_port(),
                    visualizer.get_input_port(0))

    # Add a camera, too, though no controller or estimator
    # will consume the output of it.
    # - Add frame for camera fixture.
    '''
    camera_frame = RigidBodyFrame(
        name="rgbd camera frame", body=rbt.world(),
        xyz=[2.5, 0., 1.5], rpy=[-np.pi/4, 0., -np.pi])
    rbt.addFrame(camera_frame)
    camera = builder.AddSystem(
        RgbdCamera(name="camera", tree=rbt, frame=camera_frame,
                   z_near=0.5, z_far=2.0, fov_y=np.pi / 4,
                   width=320, height=240,
                   show_window=False))
    builder.Connect(rbplant_sys.state_output_port(),
                    camera.get_input_port(0))

    camera_meshcat_visualizer = builder.AddSystem(
        kuka_utils.RgbdCameraMeshcatVisualizer(camera, rbt))
    builder.Connect(camera.depth_image_output_port(),
                    camera_meshcat_visualizer.camera_input_port)
    builder.Connect(rbplant_sys.state_output_port(),
                    camera_meshcat_visualizer.state_input_port)
    '''

    # Hook up loggers for the robot state, the robot setpoints,
    # and the torque inputs.
    def log_output(output_port, rate):
        logger = builder.AddSystem(SignalLogger(output_port.size()))
        logger._DeclarePeriodicPublish(1. / rate, 0.0)
        builder.Connect(output_port, logger.get_input_port(0))
        return logger
    state_log = log_output(rbplant_sys.get_output_port(0), 60.)
    setpoint_log = log_output(
        manip_state_machine.kuka_setpoint_output_port, 60.)
    kuka_control_log = log_output(
        kuka_controller.get_output_port(0), 60.)

    # Done! Compile it all together and visualize it.
    diagram = builder.Build()
    kuka_utils.render_system_with_graphviz(diagram, "view.gv")

    # Create a simulator for it.
    simulator = Simulator(diagram)
    simulator.Initialize()
    simulator.set_target_realtime_rate(1.0)
    # Simulator time steps will be very small, so don't
    # force the rest of the system to update every single time.
    simulator.set_publish_every_time_step(False)

    # The simulator simulates forward from a given Context,
    # so we adjust the simulator's initial Context to set up
    # the initial state.
    state = simulator.get_mutable_context().\
        get_mutable_continuous_state_vector()
    initial_state = np.zeros(state.size())
    initial_state[0:q0.shape[0]] = q0
    state.SetFromVector(initial_state)

    # From iiwa_wsg_simulation.cc:
    # When using the default RK3 integrator, the simulation stops
    # advancing once the gripper grasps the box.  Grasping makes the
    # problem computationally stiff, which brings the default RK3
    # integrator to its knees.
    timestep = 0.0001
    simulator.reset_integrator(
        RungeKutta2Integrator(diagram, timestep,
                              simulator.get_mutable_context()))

    # This kicks off simulation. Most of the run time will be spent
    # in this call.
    try:
        simulator.StepTo(args.duration)
    except StopIteration:
        print "Terminated early"
    print("Final state: ", state.CopyToVector())
    end_time = simulator.get_mutable_context().get_time()

    if args.animate_forever:
        try:
            while(1):
                mrbv.animate(state_log)
        except Exception as e:
            print "Fail ", e
    elif args.hacky_save_video:
        filename = "seed_%d.ogg" % int_seed
        import subprocess
        p = subprocess.Popen(
            ("byzanz-record -x 3833 -y 703 -w 1616 -h 977 "
             "%s --delay=0 -d %d -v" % (filename, end_time+1)).split(" "))
        mrbv.animate(state_log)
        p.wait()

    if args.show_plots:
        # Do some plotting to show off accessing signal logger data.
        nq = rbt.get_num_positions()
        plt.figure()
        plt.subplot(3, 1, 1)
        dims_to_draw = range(7)
        color = iter(plt.cm.rainbow(np.linspace(0, 1, 7)))
        for i in dims_to_draw:
            colorthis = next(color)
            plt.plot(state_log.sample_times(),
                     state_log.data()[i, :],
                     color=colorthis,
                     linestyle='solid',
                     label="q[%d]" % i)
            plt.plot(setpoint_log.sample_times(),
                     setpoint_log.data()[i, :],
                     color=colorthis,
                     linestyle='dashed',
                     label="q_des[%d]" % i)
        plt.ylabel("m")
        plt.grid(True)
        plt.legend(bbox_to_anchor=(1.05, 1), loc=2, borderaxespad=0.)

        plt.subplot(3, 1, 2)
        color = iter(plt.cm.rainbow(np.linspace(0, 1, 7)))
        for i in dims_to_draw:
            colorthis = next(color)
            plt.plot(state_log.sample_times(),
                     state_log.data()[nq + i, :],
                     color=colorthis,
                     linestyle='solid',
                     label="v[%d]" % i)
            plt.plot(setpoint_log.sample_times(),
                     setpoint_log.data()[nq + i, :],
                     color=colorthis,
                     linestyle='dashed',
                     label="v_des[%d]" % i)
        plt.ylabel("m/s")
        plt.grid(True)
        plt.legend(bbox_to_anchor=(1.05, 1), loc=2, borderaxespad=0.)

        plt.subplot(3, 1, 3)
        color = iter(plt.cm.rainbow(np.linspace(0, 1, 7)))
        for i in dims_to_draw:
            colorthis = next(color)
            plt.plot(kuka_control_log.sample_times(),
                     kuka_control_log.data()[i, :],
                     color=colorthis,
                     linestyle=':',
                     label="u[%d]" % i)
        plt.xlabel("t")
        plt.ylabel("N/m")
        plt.grid(True)
        plt.legend(bbox_to_anchor=(1.05, 1), loc=2, borderaxespad=0.)
        plt.show()

    if meshcat_server_p is not None:
        meshcat_server_p.kill()
        meshcat_server_p.wait()
