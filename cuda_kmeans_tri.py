import pycuda.driver as cuda
import pycuda.autoinit
import pycuda.gpuarray as gpuarray
from pycuda.compiler import SourceModule
from pycuda.reduction import ReductionKernel
from cpu_kmeans import kmeans_cpu
from cpu_kmeans import assign_cpu
from cpu_kmeans import calc_cpu

import numpy as np
import math
import time

import mods2

VERBOSE = 0
PRINT_TIMES = 0



#------------------------------------------------------------------------------------
#                kmeans using triangle inequality algorithm on the gpu
#------------------------------------------------------------------------------------

def trikmeans_gpu(data, clusters, iterations, return_times = 0):
    # trikmeans_gpu(data, clusters, iterations) returns (clusters, labels)
    
    # kmeans using triangle inequality algorithm and cuda
    # input arguments are the data, intial cluster values, and number of iterations to repeat
    # The shape of data is (nDim, nPts) where nDim = # of dimensions in the data and
    # nPts = number of data points
    # The shape of clustrs is (nDim, nClusters) 
    #
    # The return values are the updated clusters and labels for the data
    
    #---------------------------------------------------------------
    #                   get problem parameters
    #---------------------------------------------------------------
    (nDim, nPts) = data.shape
    nClusters = clusters.shape[1]

    
    #---------------------------------------------------------------
    #            set calculation control variables
    #---------------------------------------------------------------
    useTextureForData = 1
    
    
    # block and grid sizes for the ccdist kernel (also for hdclosest)
    blocksize_ccdist = min(512, 16*(1+(nClusters-1)/16))
    gridsize_ccdist = 1 + (nClusters-1)/blocksize_ccdist
    
    #block and grid sizes for the init module
    threads_desired = 16*(1+(max(nPts, nDim*nClusters)-1)/16)
    blocksize_init = min(512, threads_desired) 
    gridsize_init = 1 + (threads_desired - 1)/blocksize_init
    
    #block and grid sizes for the step3 module
    blocksize_step3 = blocksize_init
    gridsize_step3 = gridsize_init
    
    #block and grid sizes for the step4 module
    for blocksize_step4_x in range(32, 512, 32):
        if blocksize_step4_x >= nClusters:
            break;
    blocksize_step4_y = min(nDim, 512/blocksize_step4_x)
    gridsize_step4_x = 1 + (nClusters-1)/blocksize_step4_x
    gridsize_step4_y = 1 + (nDim-1)/blocksize_step4_y
    
    #block and grid sizes for the calc_movement module
    blocksize_calcm = blocksize_step4_x
    gridsize_calcm = gridsize_step4_x    
    
    #block and grid sizes for the step56 module
    blocksize_step56 = blocksize_init
    gridsize_step56 = gridsize_init
    
    
    
    #---------------------------------------------------------------
    #                    prepare source modules
    #---------------------------------------------------------------
    t1 = time.time()
    mod_ccdist = mods2.get_ccdist_module(nDim, nPts, nClusters, blocksize_ccdist, blocksize_init, 
                                        blocksize_step4_x, blocksize_step4_y, blocksize_step56,
                                        useTextureForData)

    #mod_step56 = mods2.get_step56_module(nDim, nPts, nClusters, blocksize_step56)
    
    ccdist = mod_ccdist.get_function("ccdist")
    calc_hdclosest = mod_ccdist.get_function("calc_hdclosest")
    init = mod_ccdist.get_function("init")
    step3 = mod_ccdist.get_function("step3")
    step4 = mod_ccdist.get_function("step4")
    calc_movement = mod_ccdist.get_function("calc_movement")
    step56 = mod_ccdist.get_function("step56")
    pycuda.autoinit.context.synchronize()
    t2 = time.time()
    module_time = t2-t1

    #---------------------------------------------------------------
    #                    setup data on GPU
    #---------------------------------------------------------------
    t1 = time.time()

    data = np.array(data).astype(np.float32)
    clusters = np.array(clusters).astype(np.float32)
    
    if useTextureForData:
        # copy the data to the texture
        texrefData = mod_ccdist.get_texref("texData")
        cuda.matrix_to_texref(data, texrefData, order="F")
    else:
        gpu_data = gpuarray.to_gpu(data)


    gpu_clusters = gpuarray.to_gpu(clusters)
    gpu_assignments = gpuarray.zeros((nPts,), np.int32)         # cluster assignment
    gpu_lower = gpuarray.zeros((nClusters, nPts), np.float32)   # lower bounds on distance between 
                                                                # point and each cluster
    gpu_upper = gpuarray.zeros((nPts,), np.float32)             # upper bounds on distance between
                                                                # point and any cluster
    gpu_ccdist = gpuarray.zeros((nClusters, nClusters), np.float32)    # cluster-cluster distances
    gpu_hdClosest = gpuarray.zeros((nClusters,), np.float32)    # half distance to closest
    gpu_hdClosest.fill(1.0e10)  # set to large value // **TODO**  get the acutal float max
    gpu_badUpper = gpuarray.zeros((nPts,), np.int32)   # flag to indicate upper bound needs recalc
    gpu_clusters2 = gpuarray.zeros((nDim, nClusters), np.float32);
    gpu_cluster_movement = gpuarray.zeros((nClusters,), np.float32);
    
    gpu_cluster_changed = gpuarray.zeros((nClusters,), np.int32)
    
    pycuda.autoinit.context.synchronize()
    t2 = time.time()
    data_time = t2-t1
    
    #---------------------------------------------------------------
    #                    do calculations
    #---------------------------------------------------------------
    ccdist_time = 0.
    hdclosest_time = 0.
    init_time = 0.
    step3_time = 0.
    step4_time = 0.
    step56_time = 0.

    t1 = time.time()
    ccdist(gpu_clusters, gpu_ccdist, gpu_hdClosest,
             block = (blocksize_ccdist, 1, 1),
             grid = (gridsize_ccdist, 1))
    pycuda.autoinit.context.synchronize()
    t2 = time.time()
    ccdist_time += t2-t1
    
    t1 = time.time()
    calc_hdclosest(gpu_ccdist, gpu_hdClosest,
            block = (blocksize_ccdist, 1, 1),
            grid = (gridsize_ccdist, 1))
    pycuda.autoinit.context.synchronize()
    t2 = time.time()
    hdclosest_time += t2-t1
    
    t1 = time.time()
    if useTextureForData:
        init(gpu_clusters, gpu_ccdist, gpu_hdClosest, gpu_assignments, 
                gpu_lower, gpu_upper,
                block = (blocksize_init, 1, 1),
                grid = (gridsize_init, 1),
                texrefs=[texrefData])
    else:
        init(gpu_data, gpu_clusters, gpu_ccdist, gpu_hdClosest, gpu_assignments, 
                gpu_lower, gpu_upper,
                block = (blocksize_init, 1, 1),
                grid = (gridsize_init, 1))
    pycuda.autoinit.context.synchronize()
    t2 = time.time()
    init_time += t2-t1

    """    
    print "data"
    print data
    print "gpu_dataout"
    print gpu_dataout
    return 1
    """

    for i in range(iterations):
    
        if i>0:
            t1 = time.time()
            ccdist(gpu_clusters, gpu_ccdist, gpu_hdClosest,
                     block = (blocksize_ccdist, 1, 1),
                     grid = (gridsize_ccdist, 1))
            pycuda.autoinit.context.synchronize()
            t2 = time.time()
            ccdist_time += t2-t1
            
            t1 = time.time()
            calc_hdclosest(gpu_ccdist, gpu_hdClosest,
                    block = (blocksize_ccdist, 1, 1),
                    grid = (gridsize_ccdist, 1))
            pycuda.autoinit.context.synchronize()
            t2 = time.time()
            hdclosest_time += t2-t1
            
        """
        print "Just before step 3=========================================="
        print "gpu_clusters"
        print gpu_clusters
        print "gpu_ccdist"
        print gpu_ccdist
        print "gpu_hdClosest"
        print gpu_hdClosest
        print "gpu_assignments"
        print gpu_assignments
        print "gpu_lower"
        print gpu_lower
        print "gpu_upper"
        print gpu_upper
        print "gpu_badUpper"
        print gpu_badUpper
        """
        
        t1 = time.time()
        gpu_cluster_changed.fill(0)
        if useTextureForData:
            step3(gpu_clusters, gpu_ccdist, gpu_hdClosest, gpu_assignments,
                    gpu_lower, gpu_upper, gpu_badUpper, gpu_cluster_changed,
                    block = (blocksize_step3, 1, 1),
                    grid = (gridsize_step3, 1),
                    texrefs=[texrefData])
        else:
            step3(gpu_data, gpu_clusters, gpu_ccdist, gpu_hdClosest, gpu_assignments,
                    gpu_lower, gpu_upper, gpu_badUpper,  gpu_cluster_changed,
                    block = (blocksize_step3, 1, 1),
                    grid = (gridsize_step3, 1))
        
        pycuda.autoinit.context.synchronize()
        t2 = time.time()
        step3_time += t2-t1
        
        """
        print "gpu_cluster_changed"
        print gpu_cluster_changed.get()
        """
        
        """
        print "Just before step 4=========================================="
        print "gpu_assignments"
        print gpu_assignments
        print "gpu_lower"
        print gpu_lower
        print "gpu_upper"
        print gpu_upper
        print "gpu_badUpper"
        print gpu_badUpper
        """        
    
        t1 = time.time()
        
        if useTextureForData:
            step4(gpu_clusters, gpu_clusters2, gpu_assignments, gpu_cluster_movement,
                gpu_cluster_changed,
                block = (blocksize_step4_x, blocksize_step4_y, 1),
                grid = (gridsize_step4_x, gridsize_step4_y),
                texrefs=[texrefData])
        else:
            step4(gpu_data, gpu_clusters, gpu_clusters2, gpu_assignments, gpu_cluster_movement,
                gpu_cluster_changed,
                block = (blocksize_step4_x, blocksize_step4_y, 1),
                grid = (gridsize_step4_x, gridsize_step4_y))
        
        #"""
        calc_movement(gpu_clusters, gpu_clusters2, gpu_cluster_movement,
                block = (blocksize_calcm, 1, 1),
                grid = (gridsize_calcm, 1))
        #"""
        
        pycuda.autoinit.context.synchronize()
        t2 = time.time()
        step4_time += t2-t1
        
        """
        print "Just before step 5=========================================="
        print "gpu_cluste_movement"
        print gpu_cluster_movement
        print "gpu_clusters"
        print gpu_clusters2
        """
    
        t1 = time.time() #------------------------------------------------------------------

        if useTextureForData:
            step56(gpu_assignments, gpu_lower, gpu_upper, 
                    gpu_cluster_movement, gpu_badUpper,
                    block = (blocksize_step56, 1, 1),
                    grid = (gridsize_step56, 1),
                    texrefs=[texrefData])
        else:
            step56(gpu_assignments, gpu_lower, gpu_upper, 
                    gpu_cluster_movement, gpu_badUpper,
                    block = (blocksize_step56, 1, 1),
                    grid = (gridsize_step56, 1))
                    
        pycuda.autoinit.context.synchronize()
        t2 = time.time()
        step56_time += t2-t1 #--------------------------------------------------------------
        
        """
        print "Just after step 6=========================================="
        print "gpu_lower"
        print gpu_lower
        print "gpu_upper"
        print gpu_upper
        print "gpu_badUpper"
        print gpu_badUpper
        """

        #if gpuarray.sum(gpu_cluster_movement).get() < 1.e-7:
            #print "No change in clusters!"
            #break
            
        # prepare for next iteration
        temp = gpu_clusters
        gpu_clusters = gpu_clusters2
        gpu_clusters2 = temp
        
    if return_times:
        return gpu_ccdist, gpu_hdClosest, gpu_assignments, gpu_lower, gpu_upper, \
                gpu_clusters.get(), gpu_cluster_movement, \
                data_time, module_time, init_time, \
                ccdist_time/iterations, hdclosest_time/iterations, \
                step3_time/iterations, step4_time/iterations, step56_time/iterations
    else:
        return gpu_clusters.get(), gpu_assignments.get()







#--------------------------------------------------------------------------------------------
#                           testing functions
#--------------------------------------------------------------------------------------------
    
def run_tests1(nTests, nPts, nDim, nClusters, nReps=1, verbose = VERBOSE, print_times = PRINT_TIMES):
    # run_tests(nTests, nPts, nDim, nClusters, nReps [, verbose [, print_times]]
    
    if nReps > 1:
        print "This method only runs test for nReps == 1"
        return 1
        
    # Generate nPts random data elements with nDim dimensions and nCluster random clusters,
    # then run kmeans for nReps and compare gpu and cpu results.  This is repeated nTests times
    cpu_time = 0.
    gpu_time = 0.
    
    gpu_data_time = 0.
    gpu_module_time = 0.
    gpu_ccdist_time = 0.
    gpu_hdclosest_time = 0.
    gpu_init_time = 0.
    gpu_step3_time = 0.
    gpu_step4_time = 0.
    gpu_step56_time = 0.

    np.random.seed(100)
    data = np.random.rand(nDim, nPts).astype(np.float32)
    clusters = np.random.rand(nDim, nClusters).astype(np.float32)

    if verbose:
        print "data"
        print data
        print "\nclusters"
        print clusters

    nErrors = 0

    # repeat this test nTests times
    for iTest in range(nTests):
    
        #run the gpu algorithm
        t1 = time.time()
        (gpu_ccdist, gpu_hdClosest, gpu_assignments, gpu_lower, gpu_upper, \
            gpu_clusters2, gpu_cluster_movement, \
            data_time, module_time, init_time, ccdist_time, hdclosest_time, \
            step3_time, step4_time, step56_time) = \
            trikmeans_gpu(data, clusters, nReps, 1)
        t2 = time.time()        
        gpu_time += t2-t1
        gpu_data_time += data_time
        gpu_module_time += module_time
        gpu_ccdist_time += ccdist_time
        gpu_hdclosest_time += hdclosest_time
        gpu_init_time += init_time
        gpu_step3_time += step3_time
        gpu_step4_time += step4_time
        gpu_step56_time += step56_time
        
        if verbose:
            print "------------------------ gpu results ------------------------"
            print "cluster-cluster distances"
            print gpu_ccdist
            print "half distance to closest"
            print gpu_hdClosest
            print "gpu time = ", t2-t1
            print "gpu_assignments"
            print gpu_assignments
            print "gpu_lower"
            print gpu_lower
            print "gpu_upper"
            print gpu_upper
            print "gpu_clusters2"
            print gpu_clusters2
            print "-------------------------------------------------------------"
            

        # check ccdist and hdClosest
        ccdist = np.array(gpu_ccdist.get())
        hdClosest = np.array(gpu_hdClosest.get())
        
        t1 = time.time()
        cpu_ccdist = 0.5 * np.sqrt(((clusters[:,:,np.newaxis]-clusters[:,np.newaxis,:])**2).sum(0))
        t2 = time.time()
        cpu_ccdist_time = t2-t1
        
        if verbose:
            print "cpu_ccdist"
            print cpu_ccdist
        
        error = np.abs(cpu_ccdist - ccdist)
        if np.max(error) > 1e-7 * nDim * 2:
            print "iteration", iTest,
            print "***ERROR*** max ccdist error =", np.max(error)
            nErrors += 1
        if verbose:
            print "average ccdist error =", np.mean(error)
            print "max ccdist error     =", np.max(error)
        
        t1 = time.time()
        cpu_ccdist[cpu_ccdist == 0.] = 1e10
        good_hdClosest = np.min(cpu_ccdist, 0)
        t2 = time.time()
        cpu_hdclosest_time = t2-t1
        
        if verbose:
            print "good_hdClosest"
            print good_hdClosest
        err = np.abs(good_hdClosest - hdClosest)
        if np.max(err) > 1e-7 * nDim:
            print "***ERROR*** max hdClosest error =", np.max(err)
            nErrors += 1
        if verbose:
            print "errors on hdClosest"
            print err
            print "max error on hdClosest =", np.max(err)
    
    
        # calculate cpu initial assignments
        t1 = time.time()
        cpu_assign = assign_cpu(data, clusters)
        t2 = time.time()
        cpu_assign_time = t2-t1
        
        if verbose:
            print "cpu assignments"
            print cpu_assign
            print "gpu assignments"
            print gpu_assignments
            print "gpu new clusters"
            print gpu_clusters2
            
        differences = sum(gpu_assignments.get() - cpu_assign)
        if(differences > 0):
            nErrors += 1
            print differences, "errors in initial assignment"
        else:
            if verbose:
                print "initial cluster assignments match"
    
        # calculate the number of data points in each cluster
        c = np.arange(nClusters)
        c_counts = np.sum(cpu_assign.reshape(nPts,1) == c, axis=0)

        # calculate cpu new cluster values:
        t1 = time.time()
        cpu_new_clusters = calc_cpu(data, cpu_assign, clusters)
        t2 = time.time()
        cpu_calc_time = t2-t1
        
        if verbose:
            print "cpu new clusters"
            print cpu_new_clusters
        
        diff = np.max(np.abs(gpu_clusters2 - cpu_new_clusters))
        if diff > 1e-7 * max(c_counts) or math.isnan(diff):
            iDiff = np.arange(nClusters)[((gpu_clusters2 - cpu_new_clusters)**2).sum(0) > 1e-7]
            print "clusters that differ:"
            print iDiff
            nErrors += 1
            if verbose:
                print "Test",iTest,"*** ERROR *** max diff was", diff
                print 
        else:
            if verbose:
                print "Test", iTest, "OK"
        
        #check if the cluster movement values are correct
        cpu_cluster_movement = np.sqrt(((clusters - cpu_new_clusters)**2).sum(0))
        diff = np.max(np.abs(cpu_cluster_movement - gpu_cluster_movement.get()))
        if diff > 1e-7 * nDim:
            print "*** ERROR *** max cluster movement error =", diff
        if verbose:
            print "cpu cluster movements"
            print cpu_cluster_movement
            print "gpu cluster movements"
            print gpu_cluster_movement
            print "max diff in cluster movements is", diff
        
        cpu_time = cpu_assign_time + cpu_calc_time
    

    if print_times:
        print "\n---------------------------------------------"
        print "nPts      =", nPts
        print "nDim      =", nDim
        print "nClusters =", nClusters
        print "nReps     =", nReps
        print "average cpu time (ms) =", cpu_time/nTests*1000.
        print "     assign time (ms) =", cpu_assign_time/nTests*1000.
        if nReps == 1:
            print "       calc time (ms) =", cpu_calc_time/nTests*1000.
            print "average gpu time (ms) =", gpu_time/nTests*1000.
        else:
            print "       calc time (ms) ="
            print "average gpu time (ms) ="
        print "       data time (ms) =", gpu_data_time/nTests*1000.
        print "     module time (ms) =", gpu_module_time/nTests*1000.
        print "       init time (ms) =", gpu_init_time/nTests*1000.        
        print "     ccdist time (ms) =", gpu_ccdist_time/nTests*1000.        
        print "  hdclosest time (ms) =", gpu_hdclosest_time/nTests*1000.        
        print "      step3 time (ms) =", gpu_step3_time/nTests*1000.        
        print "      step4 time (ms) =", gpu_step4_time/nTests*1000.        
        print "     step56 time (ms) =", gpu_step56_time/nTests*1000.        
        print "---------------------------------------------"

    return nErrors


def verify_assignments(gpu_assign, cpu_assign, data, gpu_clusters, cpu_clusters, verbose = 0, iTest = -1): 
    # check that assignments are equal

    """
    print "verify_assignments"
    print "gpu_assign", gpu_assign, "is type", type(gpu_assign)
    print "gpu_assign", cpu_assign, "is type", type(cpu_assign)
    """
    differences = sum(gpu_assign != cpu_assign)
    # print "differences =", differences
    error = 0
    if(differences > 0):
        error = 1
        if verbose:
            if iTest >= 0:
                print "Test", iTest,
            print "*** ERROR ***", differences, "differences"
            iDiff = np.arange(gpu_assign.shape[0])[gpu_assign != cpu_assign]
            print "iDiff", iDiff
            for ii in iDiff:
                print "data point is", data[:,ii]
                print "cpu assigned to", cpu_assign[ii]
                print "   with center at (cpu)", cpu_clusters[:,cpu_assign[ii]]
                print "   with center at (gpu)", gpu_clusters[:,cpu_assign[ii]]
                print "gpu assigned to", gpu_assign[ii]
                print "   with center at (cpu)", cpu_clusters[:,gpu_assign[ii]]
                print "   with center at (gpu)", gpu_clusters[:, gpu_assign[ii]]
                print ""
                print "cpu calculated distances:"
                print "   from point", ii, "to:"
                print "      cluster", cpu_assign[ii], "is", np.sqrt(np.sum((data[:,ii]-cpu_clusters[:,cpu_assign[ii]])**2))
                print "      cluster", gpu_assign[ii], "is", np.sqrt(np.sum((data[:,ii]-cpu_clusters[:,gpu_assign[ii]])**2))
                print "gpu calculated distances:"
                print "   from point", ii, "to:"
                print "      cluster", cpu_assign[ii], "is", np.sqrt(np.sum((data[:,ii]-gpu_clusters[:,cpu_assign[ii]])**2))
                print "      cluster", gpu_assign[ii], "is", np.sqrt(np.sum((data[:,ii]-gpu_clusters[:,gpu_assign[ii]])**2))
    else:
        if verbose:
            if iTest >= 0:
                print "Test", iTest,
            print "Cluster assignment is OK"
    return error

def verify_clusters(gpu_clusters, cpu_clusters, cpu_assign, verbose = 0, iTest = -1):
    # check that clusters are equal
    error = 0
    
    # calculate the number of data points in each cluster
    nPts = cpu_assign.shape[0]
    nClusters = cpu_clusters.shape[1]
    c = np.arange(nClusters)
    c_counts = np.sum(cpu_assign.reshape(nPts,1) == c, axis=0)
    
    err = np.abs(gpu_clusters - cpu_clusters)
    diff = np.max(err)
    
    if verbose:
        print "max error in cluster centers is", diff
        print "avg error in cluster centers is", np.mean(err)
    
    allowable_diff = max(c_counts) * 1e-7
    if diff > allowable_diff or math.isnan(diff):
        error = 1
        iDiff = np.arange(nClusters)[((gpu_clusters - cpu_clusters)**2).sum(0) > allowable_diff]
        if verbose:
            print "clusters that differ:"
            print iDiff
            if iTest >= 0:
                print "Test",iTest,
            print "*** ERROR *** max diff was", diff
            print 
    else:
        if verbose:
            if iTest >= 0:
                print "Test", iTest,
            print "Clusters are OK"
        
    return error


def run_tests(nTests, nPts, nDim, nClusters, nReps=1, verbose = VERBOSE, print_times = PRINT_TIMES):
    # run_tests(nTests, nPts, nDim, nClusters, nReps [, verbose [, print_times]]
    
    # Generate nPts random data elements with nDim dimensions and nCluster random clusters,
    # then run kmeans for nReps and compare gpu and cpu results.  This is repeated nTests times
    cpu_time = 0.
    gpu_time = 0.
    
    gpu_data_time = 0.
    gpu_module_time = 0.
    gpu_ccdist_time = 0.
    gpu_hdclosest_time = 0.
    gpu_init_time = 0.
    gpu_step3_time = 0.
    gpu_step4_time = 0.
    gpu_step56_time = 0.

    np.random.seed(100)
    data = np.random.rand(nDim, nPts).astype(np.float32)
    clusters = np.random.rand(nDim, nClusters).astype(np.float32)

    if verbose:
        print "data"
        print data
        print "\nclusters"
        print clusters

    nErrors = 0

    # repeat this test nTests times
    for iTest in range(nTests):
    
        """
        #run the cpu algorithm
        t1 = time.time()
        (cpu_clusters, cpu_assign) = kmeans_cpu(data, clusters, nReps)
        cpu_assign.shape = (nPts,)
        t2 = time.time()
        cpu_time += t2-t1
        
        if verbose:
            print "------------------------ cpu results ------------------------"
            print "cpu_assignments"
            print cpu_assign
            print "cpu_clusters"
            print cpu_clusters
            print "-------------------------------------------------------------"
        """
        
        #run the gpu algorithm
        t1 = time.time()
        (gpu_ccdist, gpu_hdClosest, gpu_assign, gpu_lower, gpu_upper, \
            gpu_clusters, gpu_cluster_movement, \
            data_time, module_time, init_time, ccdist_time, hdclosest_time, \
            step3_time, step4_time, step56_time) = \
            trikmeans_gpu(data, clusters, nReps, 1)
        t2 = time.time()        
        gpu_time += t2-t1
        gpu_data_time += data_time
        gpu_module_time += module_time
        gpu_ccdist_time += ccdist_time
        gpu_hdclosest_time += hdclosest_time
        gpu_init_time += init_time
        gpu_step3_time += step3_time
        gpu_step4_time += step4_time
        gpu_step56_time += step56_time
        
        if verbose:
            print "------------------------ gpu results ------------------------"
            print "gpu_assignments"
            print gpu_assign
            print "gpu_clusters"
            print gpu_clusters
            print "-------------------------------------------------------------"
            

        """
        # calculate the number of data points in each cluster
        c = np.arange(nClusters)
        c_counts = np.sum(cpu_assign.reshape(nPts,1) == c, axis=0)

        # verify the results...
        nErrors += verify_assignments(gpu_assign.get(), cpu_assign, data, gpu_clusters, cpu_clusters, verbose, iTest)
        nErrors += verify_clusters(gpu_clusters, cpu_clusters, cpu_assign, verbose, iTest)
        """

    if print_times:
        print "\n---------------------------------------------"
        print "nPts      =", nPts
        print "nDim      =", nDim
        print "nClusters =", nClusters
        print "nReps     =", nReps
        #print "average cpu time (ms) =", cpu_time/nTests*1000.
        print "average cpu time (ms) = N/A"
        print "average gpu time (ms) =", gpu_time/nTests*1000.
        print "       data time (ms) =", gpu_data_time/nTests*1000.
        print "     module time (ms) =", gpu_module_time/nTests*1000.
        print "       init time (ms) =", gpu_init_time/nTests*1000.        
        print "     ccdist time (ms) =", gpu_ccdist_time/nTests*1000.        
        print "  hdclosest time (ms) =", gpu_hdclosest_time/nTests*1000.        
        print "      step3 time (ms) =", gpu_step3_time/nTests*1000.        
        print "      step4 time (ms) =", gpu_step4_time/nTests*1000.        
        print "     step56 time (ms) =", gpu_step56_time/nTests*1000.        
        print "---------------------------------------------"

    return nErrors


#----------------------------------------------------------------------------------------
#                           multi-tests
#----------------------------------------------------------------------------------------

def quiet_run(nTests, nPts, nDim, nClusters, nReps, ptimes = PRINT_TIMES):
    # quiet_run(nTests, nPts, nDim, nClusters, nReps [, ptimes]):
    print "[TEST]({0:3},{1:8},{2:5},{3:5}, {4:5})...".format(nTests, nPts, nDim, nClusters, nReps),
    try:
        if run_tests(nTests, nPts, nDim, nClusters, nReps, verbose = 0, print_times = ptimes) == 0:
            print "OK"
        else:
            print "*** ERROR ***"
    except cuda.LaunchError:
        print "launch error"
    
def quiet_runs(nTest_list, nPts_list, nDim_list, nClusters_list, nRep_list, print_it = PRINT_TIMES):
    # quiet_runs(nTest_list, nPts_list, nDim_list, nClusters_list [, print_it]):
    # when number of tests is -1, it will be calculated based on the size of the problem
    for t in nTest_list:
        for pts in nPts_list:
            for dim in nDim_list:
                for clst in nClusters_list:
                    if clst > pts or clst * dim > 4000:
                        continue
                    for rep in nRep_list:
                        if t < 0:
                            tt = max(1, min(10, 10000000/(pts*dim*clst)))
                        else:
                            tt = t
                        quiet_run(tt, pts, dim, clst, rep, ptimes = print_it);

def run_all(pFlag = 1):
    quiet_run(1, 10, 4, 3, 1, ptimes = pFlag)
    quiet_run(1, 1000, 60, 20, 1, ptimes = pFlag)
    quiet_run(1, 100000, 60, 20, 1, ptimes = pFlag)
    quiet_run(1, 10000, 600, 5, 1, ptimes = pFlag)
    quiet_run(1, 10000, 5, 600, 1, ptimes = pFlag)
    quiet_run(1, 100, 5, 600, 1, ptimes = pFlag)
    quiet_run(1, 100, 600, 5, 1, ptimes = pFlag)
    quiet_run(1, 10, 20, 30, 1, ptimes = pFlag)
    quiet_run(1, 10, 4, 3, 10, ptimes = pFlag)
    quiet_run(1, 1000, 60, 20, 10, ptimes = pFlag)
    quiet_run(1, 10000, 60, 20, 10, ptimes = pFlag)
    quiet_run(1, 1000, 600, 5, 10, ptimes = pFlag)
    quiet_run(1, 1000, 5, 600, 10, ptimes = pFlag)
    quiet_run(1, 100, 5, 600, 10, ptimes = pFlag)
    quiet_run(1, 100, 600, 5, 10, ptimes = pFlag)
    quiet_run(1, 10, 20, 30, 10, ptimes = pFlag)


def run_reps(pFlag = 1):
    quiet_run(1, 10, 4, 3, 5, ptimes = pFlag)
    quiet_run(1, 1000, 60, 20, 5, ptimes = pFlag)
    quiet_run(1, 50000, 60, 20, 5, ptimes = pFlag)
    quiet_run(1, 10000, 600, 5, 5, ptimes = pFlag)
    quiet_run(1, 10000, 5, 600, 5, ptimes = pFlag)
    quiet_run(1, 100, 5, 600, 5, ptimes = pFlag)
    quiet_run(1, 100, 600, 5, 5, ptimes = pFlag)
    quiet_run(1, 10, 20, 30, 5, ptimes = pFlag)
    
def timings(t = 0):
    # run a bunch of tests with optional timing
    quiet_runs([1], [10, 100, 1000, 10000], [2, 8, 32, 600], [3, 9, 27, 600], [1], print_it = t)
    
def quickTimes(nReps = 5):
    if quickRun() > 0:
        print "***ERROR***"
    else:
        quiet_run(3, 1000, 60, 20, nReps, 1)
        quiet_run(3, 1000, 600, 2, nReps, 1)
        quiet_run(3, 1000, 6, 200, nReps, 1)
        quiet_run(3, 10000, 60, 20, nReps, 1)
        quiet_run(3, 10000, 600, 2, nReps, 1)
        quiet_run(3, 10000, 6, 200, nReps, 1)
        #quiet_run(3, 100000, 6, 20, nReps, 1)
        quiet_run(3, 30000, 6, 20, nReps, 1)

def quickRun():
    # run to make sure answers have not changed
    nErrors = run_tests1(1, 10, 3, 4, 1)
    nErrors += run_tests1(1, 1000, 600, 2, 1)
    nErrors += run_tests1(1, 10000, 2, 600, 1)
    return nErrors
    
if __name__ == '__main__':
    print quickRun()
    
    
    